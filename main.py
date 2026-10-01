#!/usr/bin/env python3
"""Clock — núcleo único da linguagem e das ferramentas.

main.py é o coração do projeto. Ele concentra: o interpretador de .clk,
os modules nativos, o sistema de packages, o histórico e as duas CLIs.
Os executáveis Clock e ClockInstall são apenas lançadores finos que chamam
este file; clock.py é mantido somente como compatibilidade.
"""

from __future__ import annotations

import ast
import math
import operator
import os
import shutil
import subprocess
import sys
import time
import random
import json
import urllib.parse
import urllib.request
import urllib.error
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
            raise ClockError(f"invalid expression: {source}") from exc
        return self.visit(tree.body)

    def visit(self, node: ast.AST) -> Any:
        method = getattr(self, f"visit_{type(node).__name__}", None)
        if method is None:
            raise ClockError(f"operation not allowed in expression: {type(node).__name__}")
        return method(node)

    def visit_Constant(self, node: ast.Constant) -> Any:
        if node.value is None or isinstance(node.value, (str, int, float, bool)):
            return node.value
        raise ClockError("unsupported constant")

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id in self.env:
            return self.env[node.id]
        if node.id == "true":
            return True
        if node.id == "false":
            return False
        if node.id == "null":
            return None
        raise ClockError(f"name not found: {node.id}")

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
            raise ClockError("unsupported operator")
        if isinstance(node.op, ast.BitXor):
            return bool(left) != bool(right)
        try:
            return op(left, right)
        except Exception as exc:
            raise ClockError(f"arithmetic error: {exc}") from exc

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
        raise ClockError("unsupported logical operator")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        op = self.UNARY_OPS.get(type(node.op))
        if op is None:
            raise ClockError("unsupported unary operator")
        return op(self.visit(node.operand))

    def visit_Compare(self, node: ast.Compare) -> bool:
        left = self.visit(node.left)
        for op_node, comparator in zip(node.ops, node.comparators):
            op = self.CMP_OPS.get(type(op_node))
            if op is None:
                raise ClockError("unsupported comparison")
            right = self.visit(comparator)
            try:
                ok = op(left, right)
            except Exception as exc:
                raise ClockError(f"comparison error: {exc}") from exc
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
            raise ClockError(f"invalid index: {exc}") from exc

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
            raise ClockError(f"attribute does not exist: {node.attr}") from exc

    def visit_Call(self, node: ast.Call) -> Any:
        func = self.visit(node.func)
        if not callable(func):
            raise ClockError("attempted to call something that is not a function")
        args = [self.visit(a) for a in node.args]
        if node.keywords:
            raise ClockError("named arguments are not supported yet")
        try:
            return func(*args)
        except ClockError:
            raise
        except ClockDie:
            raise
        except Exception as exc:
            raise ClockError(f"call error: {exc}") from exc


class ModuleProxy:
    """A Clock import namespace. Exported values/functions are available as attributes."""

    def __init__(self, name: str, exports: dict[str, Any], source: str = "builtin"):
        self.name = name
        self.exports = dict(exports)
        self.source = source

    def __getattr__(self, name: str) -> Any:
        if name in self.exports:
            value = self.exports[name]
            if callable(value):
                return value
            return value
        raise AttributeError(name)

    def __repr__(self) -> str:
        return f"<module {self.name}>"


class StringModule:
    @staticmethod
    def len(value: Any) -> int:
        return len(str(value))

    @staticmethod
    def upper(value: Any) -> str:
        return str(value).upper()

    @staticmethod
    def lower(value: Any) -> str:
        return str(value).lower()

    @staticmethod
    def trim(value: Any) -> str:
        return str(value).strip()

    @staticmethod
    def replace(value: Any, old: Any, new: Any) -> str:
        return str(value).replace(str(old), str(new))

    @staticmethod
    def split(value: Any, separator: Any = " ") -> list[str]:
        return str(value).split(str(separator))

    @staticmethod
    def substr(value: Any, start: Any = 0, length: Any = None) -> str:
        text = str(value)
        start_i = int(start)
        if length is None:
            return text[start_i:]
        return text[start_i:start_i + int(length)]

    @staticmethod
    def join(values: Any, separator: Any = "") -> str:
        return str(separator).join(str(v) for v in values)

    @staticmethod
    def contains(value: Any, part: Any) -> bool:
        return str(part) in str(value)

    @staticmethod
    def starts(value: Any, prefix: Any) -> bool:
        return str(value).startswith(str(prefix))

    @staticmethod
    def ends(value: Any, suffix: Any) -> bool:
        return str(value).endswith(str(suffix))

    @staticmethod
    def reverse(value: Any) -> str:
        return str(value)[::-1]


class FileEditor:
    """Mutable in-memory text editor returned by Fs.requestWrith()."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.content = ""
        except OSError as exc:
            raise ClockError(f"Fs could not open {path}: {exc}") from exc
        self.lines = self.content.splitlines()
        self.finished = False

    def addLine(self, value: Any, index: Any = None) -> SpecialValue:
        text = str(value)
        if index is None:
            self.lines.append(text)
        else:
            try:
                idx = int(index)
            except (TypeError, ValueError) as exc:
                raise ClockError("addLine: invalid index") from exc
            if idx < 0 or idx > len(self.lines):
                raise ClockError("addLine: index outside file")
            self.lines.insert(idx, text)
        return SpecialValue("silent")

    def rmvLine(self, index: Any) -> SpecialValue:
        try:
            idx = int(index)
        except (TypeError, ValueError) as exc:
            raise ClockError("rmvLine: invalid index") from exc
        if idx < 0 or idx >= len(self.lines):
            raise ClockError("rmvLine: index outside file")
        self.lines.pop(idx)
        return SpecialValue("silent")

    def mvLine(self, old_index: Any, new_index: Any) -> SpecialValue:
        try:
            old = int(old_index)
            new = int(new_index)
        except (TypeError, ValueError) as exc:
            raise ClockError("mvLine: invalid indexes") from exc
        if old < 0 or old >= len(self.lines):
            raise ClockError("mvLine: source line does not exist")
        if new < 0 or new >= len(self.lines):
            raise ClockError("mvLine: destination line does not exist")
        item = self.lines.pop(old)
        self.lines.insert(new, item)
        return SpecialValue("silent")

    def finish(self) -> SpecialValue:
        self.content = "\n".join(self.lines)
        if self.lines:
            self.content += "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(self.content, encoding="utf-8")
        except OSError as exc:
            raise ClockError(f"Fs could not save {self.path}: {exc}") from exc
        self.finished = True
        return SpecialValue("silent")

    def __repr__(self) -> str:
        return f"<file {self.path}>"


def fibonacci(n: Any) -> int:
    n = int(n)
    if n < 0:
        raise ClockError("Math.fi requires a non-negative number")
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a



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
    """Date snapshot exposed by CLASSES.DATE()."""

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
        # `moth` is intentionally kept because it is part of the Clock API.
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
        return "<DATE object>"


class DateClassProxy:
    """System class exposed as CLASSES.DATE."""

    def __init__(self, interpreter: "ClockInterpreter"):
        self.interpreter = interpreter

    def __call__(self, *args: Any) -> ClockDate:
        if args:
            raise ClockError("CLASSES.DATE does not take arguments")
        return ClockDate()

    def __repr__(self) -> str:
        return "<class CLASSES.DATE>"


@dataclass
class HistoryRecord:
    command: str
    outputs: list[str]


class HistoryEntry(str):
    """Read-only history command; direct `get` replays it."""


class HistoryController:
    """In-memory, read-only command history for one Clock execution."""

    _INTERNAL = {
        "interpreter", "entries", "recording", "replaying", "record_stack"
    }

    def __init__(self, interpreter: "ClockInterpreter"):
        object.__setattr__(self, "interpreter", interpreter)
        object.__setattr__(self, "entries", [])
        object.__setattr__(self, "recording", True)
        object.__setattr__(self, "replaying", False)
        object.__setattr__(self, "record_stack", [])

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._INTERNAL:
            object.__setattr__(self, name, value)
            return
        raise ClockError("CLASSES.HISTORY is read-only")

    def __getitem__(self, index: Any) -> HistoryEntry:
        if isinstance(index, bool) or not isinstance(index, int):
            raise ClockError("HISTORY[] requires an integer index")
        try:
            return HistoryEntry(self.entries[index].command)
        except IndexError as exc:
            raise ClockError(f"HISTORY[{index}] does not exist") from exc

    def __setitem__(self, index: Any, value: Any) -> None:
        raise ClockError("CLASSES.HISTORY is read-only")

    @property
    def START(self) -> SpecialValue:
        return SpecialValue("history_start")

    @property
    def PAUSE(self) -> SpecialValue:
        return SpecialValue("history_pause")

    def start(self) -> None:
        self.recording = True

    def pause(self) -> None:
        self.recording = False

    def add(self, command: str) -> HistoryRecord | None:
        if not self.recording or self.replaying:
            return None
        record = HistoryRecord(command, [])
        self.entries.append(record)
        return record

    def push_record(self, record: HistoryRecord | None) -> None:
        if record is not None:
            self.record_stack.append(record)

    def pop_record(self) -> None:
        if self.record_stack:
            self.record_stack.pop()

    def record_output(self, text: Any) -> None:
        if self.record_stack and not self.replaying:
            self.record_stack[-1].outputs.append(str(text))

    def replay(self, entry: HistoryEntry) -> Any:
        command = str(entry)
        self.replaying = True
        try:
            _, value, returned = self.interpreter._execute_single_command(
                self.interpreter.current_pc if self.interpreter.current_pc is not None else 0,
                command,
                [],
                len(self.interpreter.cleaned),
            )
            if returned:
                raise ClockReturn(value)
            return value
        finally:
            self.replaying = False

    def formatted(self) -> str:
        if not self.entries:
            return "History is empty."
        lines: list[str] = []
        for i, record in enumerate(self.entries):
            lines.append(f"[{i}] {record.command}")
            if record.outputs:
                for output in record.outputs:
                    lines.append(f"    ↳ {output}")
            else:
                lines.append("    ↳ (sem saída)")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return "<class CLASSES.HISTORY>"


class ClockSystem:
    """Internal system helpers retained for sleep/die implementation."""

    def __init__(self, interpreter: "ClockInterpreter"):
        self.interpreter = interpreter

    def sleep(self, milliseconds: Any = 0) -> SpecialValue:
        try:
            delay = float(milliseconds)
        except (TypeError, ValueError) as exc:
            raise ClockError("sleep requires milliseconds") from exc
        if delay < 0:
            raise ClockError("sleep does not accept negative time")
        if getattr(self.interpreter, "unsleepy", False):
            return SpecialValue("silent")
        time.sleep(delay / 1000.0)
        return SpecialValue("silent")

    def die(self, message: Any = "") -> Any:
        raise ClockDie(self.interpreter._format_value(message))

    def __repr__(self) -> str:
        return "<Clock system>"


class ExecClassProxy:
    """System class exposed as CLASSES.exec."""

    def __init__(self, interpreter: "ClockInterpreter"):
        self.interpreter = interpreter

    def __call__(self, *args: Any) -> "ExecObject":
        return ExecObject(self.interpreter)

    def __repr__(self) -> str:
        return "<class CLASSES.exec>"


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
            raise ClockError(f"terminal execution failed: {exc}") from exc
        stdout = result.stdout
        stderr = result.stderr
        if result.returncode != 0:
            detail = stderr.strip() or stdout.strip() or f"código {result.returncode}"
            raise ClockError(f"exec.execute failed: {detail}")
        return stdout.rstrip("\n")

    def __repr__(self) -> str:
        return "<exec object>"


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
        return f"<object {self.class_def.name}>"


class ClassNamespace:
    def __init__(self, interpreter: "ClockInterpreter"):
        self.interpreter = interpreter

    def __getattr__(self, name: str) -> Any:
        if name == "std":
            return StdClassProxy(self.interpreter)
        if name == "exec":
            return ExecClassProxy(self.interpreter)
        if name.lower() == "date":
            return DateClassProxy(self.interpreter)
        if name.lower() == "history":
            return self.interpreter.history
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
    COLORS = {
        "black": 30, "red": 31, "green": 32, "yellow": 33,
        "blue": 34, "magenta": 35, "cyan": 36, "white": 37,
        "gray": 90, "grey": 90, "bright-black": 90, "bright-red": 91,
        "bright-green": 92, "bright-yellow": 93, "bright-blue": 94,
        "bright-magenta": 95, "bright-cyan": 96, "bright-white": 97,
        "reset": 0,
    }

    def print(self, value: Any = "") -> SpecialValue:
        return SpecialValue("print", value)

    def color(self, value: Any = "", color: Any = "reset") -> SpecialValue:
        color_name = str(color).lower()
        if color_name not in self.COLORS:
            available = ", ".join(self.COLORS)
            raise ClockError(f"unknown color: {color_name}. Available: {available}")
        code = self.COLORS[color_name]
        return SpecialValue("color", f"\x1b[{code}m{value}\x1b[0m")

    def erro(self, value: Any = "") -> SpecialValue:
        return SpecialValue("error", value)

    def debug(self, value: Any = "") -> SpecialValue:
        return SpecialValue("debug", value)

    def __repr__(self) -> str:
        return "<object std>"


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


class BuiltinModules:
    @staticmethod
    def math() -> ModuleProxy:
        exports = {
            "sqrt": math.sqrt,
            "abs": abs,
            "pi": math.pi,
            "e": math.e,
            "fi": fibonacci,
            "floor": math.floor,
            "ceil": math.ceil,
            "round": round,
            "sin": math.sin,
            "cos": math.cos,
            "tan": math.tan,
            "log": math.log,
            "ln": math.log,
            "exp": math.exp,
            "min": min,
            "max": max,
            "random": random.random,
        }
        return ModuleProxy("Math", exports)

    @staticmethod
    def str_module() -> ModuleProxy:
        exports = {
            "len": StringModule.len,
            "upper": StringModule.upper,
            "lower": StringModule.lower,
            "trim": StringModule.trim,
            "replace": StringModule.replace,
            "split": StringModule.split,
            "substr": StringModule.substr,
            "join": StringModule.join,
            "contains": StringModule.contains,
            "starts": StringModule.starts,
            "ends": StringModule.ends,
            "reverse": StringModule.reverse,
        }
        return ModuleProxy("Str", exports)

    @staticmethod
    def fs(interpreter: "ClockInterpreter") -> ModuleProxy:
        def read(path: Any) -> str:
            p = interpreter.resolve_path(str(path))
            try:
                return p.read_text(encoding="utf-8")
            except OSError as exc:
                raise ClockError(f"Fs.read failed: {exc}") from exc

        def request_writh(path: Any) -> FileEditor:
            return FileEditor(interpreter.resolve_path(str(path)))

        return ModuleProxy("Fs", {
            "read": read,
            "requestWrith": request_writh,
            "requestWrite": request_writh,
        }, "native:Fs")


@dataclass
class Frame:
    callable_name: str
    args: list[Any]
    locals: dict[str, Any]
    me: Optional[ClockObject] = None


class PackageManager:
    """Clock package manager. The implementation lives in main.py."""

    VERSION = "0.2.0"
    OWNER = "abs-js"
    REPO = "CPM"
    BRANCH = "main"
    API_BASE = f"https://api.github.com/repos/{OWNER}/{REPO}"
    RAW_BASE = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{BRANCH}"

    def __init__(self, project_dir: Path | str = ".", *, realtime: bool = False):
        self.project_dir = Path(project_dir).resolve()
        self.install_root = self.project_dir / "ClockPacks"
        self.realtime = realtime
        self.registry_available = False
        self.registry_error = ""
        self.registry = self._load_registry()

    @staticmethod
    def _request(url: str, timeout: float = 4.0) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "ClockInstall/0.2.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}: {url}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"conexão falhou: {exc.reason}") from exc

    def _download(self, url: str, label: str, file_index: int, file_total: int) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "ClockInstall/0.2.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                total = int(response.headers.get("Content-Length", "0") or 0)
                received = 0
                chunks: list[bytes] = []
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                    if self.realtime:
                        pct = int(received * 100 / total) if total else 0
                        suffix = f" / {self._human(total)}" if total else ""
                        print(f"\r[{file_index}/{file_total}] {pct:3d}% {self._human(received)}{suffix}  {label}", end="", flush=True)
                if self.realtime:
                    suffix = f" / {self._human(total)}" if total else ""
                    print(f"\r[{file_index}/{file_total}] 100% {self._human(received)}{suffix}  {label}")
                return b"".join(chunks)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}: {url}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"conexão falhou: {exc.reason}") from exc

    @staticmethod
    def _human(value: int) -> str:
        size = float(value)
        units = ["B", "KiB", "MiB", "GiB"]
        for unit in units:
            if size < 1024 or unit == units[-1]:
                return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{int(value)} B"

    def check_connection(self) -> tuple[bool, str]:
        try:
            data = self._request(self.API_BASE)
            info = json.loads(data.decode("utf-8"))
            return True, f"connected to {info.get('full_name', self.OWNER + '/' + self.REPO)}"
        except Exception as exc:
            return False, str(exc)

    def _load_registry(self) -> dict[str, dict[str, Any]]:
        # The registry database is intentionally a yesple JSON file in CPM.
        last_error = ""
        for name in ("packages.json",):
            try:
                data = self._request(f"{self.RAW_BASE}/{name}", timeout=4.0)
                parsed = json.loads(data.decode("utf-8"))
                if isinstance(parsed, dict) and isinstance(parsed.get("packages", parsed), dict):
                    root = parsed.get("packages", parsed)
                    self.registry_available = True
                    self.registry_error = ""
                    return {str(k).lower(): dict(v) for k, v in root.items() if isinstance(v, dict)}
                last_error = f"{name}: formato inválido"
            except Exception as exc:
                last_error = str(exc)
        self.registry_error = last_error or "banco não encontrado"
        return {}

    def _registry_entry(self, package: str) -> dict[str, Any] | None:
        return self.registry.get(package.lower())

    def _package_dir_url(self, package: str) -> str:
        entry = self._registry_entry(package)
        rel = str(entry.get("path", f"packages/{package}")) if entry else f"packages/{package}"
        return f"{self.API_BASE}/contents/{urllib.parse.quote(rel, safe='/')}?ref={self.BRANCH}"

    def _list_remote_package_files(self, package: str) -> list[dict[str, Any]]:
        data = json.loads(self._request(self._package_dir_url(package)).decode("utf-8"))
        if not isinstance(data, list):
            raise RuntimeError(f"package '{package}' não possui diretório remoto válido")
        result: list[dict[str, Any]] = []
        for item in data:
            kind = item.get("type")
            name = item.get("name")
            if kind == "file":
                result.append(item)
            elif kind == "dir":
                child_url = item.get("url")
                if not child_url:
                    continue
                child = json.loads(self._request(child_url).decode("utf-8"))
                if isinstance(child, list):
                    for nested in child:
                        if nested.get("type") == "file":
                            result.append(nested)
                        elif nested.get("type") == "dir":
                            # Registry packages should be shallow enough; recurse one more level safely.
                            nested_url = nested.get("url")
                            if nested_url:
                                deep = json.loads(self._request(nested_url).decode("utf-8"))
                                if isinstance(deep, list):
                                    result.extend(x for x in deep if x.get("type") == "file")
        return result

    def install(self, package: str) -> int:
        package = str(package).strip()
        if not package or any(c in package for c in "/\\") or package in {".", ".."}:
            print("ClockInstall: invalid package name", file=sys.stderr)
            return 1
        target = self.install_root / package
        entry = self._registry_entry(package)
        try:
            files = self._list_remote_package_files(package)
        except Exception as exc:
            # If a database exists, a package missing from it is an explicit error.
            if self.registry and entry is None:
                print(f"ClockInstall: package '{package}' does not exist in the CPM database", file=sys.stderr)
                return 1
            print(f"ClockInstall: could not locate '{package}' on GitHub: {exc}", file=sys.stderr)
            return 1
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        total = len(files)
        try:
            for i, item in enumerate(files, start=1):
                rel = str(item.get("path", ""))
                if entry and entry.get("path"):
                    prefix = str(entry["path"]).rstrip("/") + "/"
                    if rel.startswith(prefix):
                        rel = rel[len(prefix):]
                else:
                    parts = rel.split("/")
                    rel = "/".join(parts[2:]) if len(parts) > 2 else parts[-1]
                if not rel:
                    continue
                url = item.get("download_url") or f"{self.RAW_BASE}/{urllib.parse.quote(str(item.get('path','')), safe='/')}"
                payload = self._download(url, rel, i, total)
                out = target / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(payload)
            manifest = target / "package.json"
            if not manifest.exists():
                manifest.write_text(json.dumps({
                    "name": package,
                    "version": "0.0.0",
                    "main": "main.clk"
                }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"ClockInstall: {package} installed at {target}")
            return 0
        except Exception as exc:
            shutil.rmtree(target, ignore_errors=True)
            print(f"ClockInstall: install error {package}: {exc}", file=sys.stderr)
            return 1

    def uninstall(self, package: str) -> int:
        target = self.install_root / str(package)
        if not target.exists():
            print(f"ClockInstall: package '{package}' is not installed", file=sys.stderr)
            return 1
        try:
            shutil.rmtree(target)
        except OSError as exc:
            print(f"ClockInstall: remove error {package}: {exc}", file=sys.stderr)
            return 1
        print(f"ClockInstall: {package} removed")
        return 0

    def list_installed(self) -> int:
        self.install_root.mkdir(parents=True, exist_ok=True)
        packages = sorted(p for p in self.install_root.iterdir() if p.is_dir())
        if not packages:
            print("No packages installed.")
            return 0
        for folder in packages:
            manifest = folder / "package.json"
            info = ""
            if manifest.exists():
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                    info = f" v{data.get('version', '?')}"
                except Exception:
                    info = " [invalid manifest]"
            print(f"{folder.name}{info}")
        return 0

    def info(self, package: str) -> int:
        installed = self.install_root / package
        entry = self._registry_entry(package)
        print(f"Package: {package}")
        print(f"Installed: {'yes' if installed.is_dir() else 'no'}")
        if not self.registry_available:
            print(f"Database: unavailable ({self.registry_error})")
        elif entry:
            print("CPM database:")
            for key, value in entry.items():
                print(f"  {key}: {value}")
        elif self.registry_available:
            print("Database: package not found")
        if installed.is_dir():
            print("Installed files:")
            for path in sorted(installed.rglob("*")):
                if path.is_file():
                    print(f"  {path.relative_to(installed)} ({path.stat().st_size} B)")
        return 0

    def diagnostic(self, package: str) -> int:
        installed = self.install_root / package
        entry = self._registry_entry(package)
        ok = True
        print(f"Diagnostic: {package}")
        if not self.registry_available:
            print(f"  [?] CPM database unavailable: {self.registry_error}")
        elif not entry:
            print("  [!] package does not exist in the CPM database")
            ok = False
        if not installed.is_dir():
            print("  [!] package is not installed")
            return 1
        manifest_path = installed / "package.json"
        if not manifest_path.is_file():
            print("  [!] package.json missing")
            ok = False
            manifest = {}
        else:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                print("  [ok] valid package.json")
            except Exception as exc:
                print(f"  [!] invalid package.json: {exc}")
                manifest = {}
                ok = False
        if entry:
            expected_version = entry.get("version")
            if expected_version and manifest.get("version") != expected_version:
                print(f"  [!] expected version {expected_version}, found {manifest.get('version')}")
                ok = False
            files = entry.get("files")
            if isinstance(files, list):
                for rel in files:
                    exists = (installed / str(rel)).is_file()
                    print(f"  {'[ok]' if exists else '[!]'} {rel}")
                    ok &= exists
        main = str(manifest.get("main", "main.clk"))
        if not (installed / main).is_file():
            print(f"  [!] main entry missing: {main}")
            ok = False
        expected_files = {"package.json", main}
        if entry and isinstance(entry.get("files"), list):
            expected_files.update(str(x) for x in entry["files"])
        extras = []
        for path in installed.rglob("*"):
            if path.is_file() and str(path.relative_to(installed)) not in expected_files:
                extras.append(str(path.relative_to(installed)))
        if extras:
            print("  [!] unexpected files:")
            for item in extras:
                print(f"      {item}")
            ok = False
        print("Result: " + ("OK" if ok else "PROBLEMS FOUND"))
        return 0 if ok else 1

    def search(self, query: str) -> int:
        query = query.lower().strip()
        found = []
        for name, entry in sorted(self.registry.items()):
            hay = " ".join([name, str(entry.get("description", "")), str(entry.get("version", ""))]).lower()
            if query in hay:
                found.append((name, entry))
        if not found:
            if self.registry:
                print("No package found in the CPM database.")
                return 0
            # Last-resort remote repository root search.
            try:
                data = json.loads(self._request(f"{self.API_BASE}/contents/packages?ref={self.BRANCH}").decode("utf-8"))
                for item in data if isinstance(data, list) else []:
                    if query in str(item.get("name", "")).lower():
                        print(item.get("name"))
                        found.append((item.get("name"), {}))
            except Exception as exc:
                print(f"ClockInstall: search unavailable: {exc}", file=sys.stderr)
                return 1
        for name, entry in found:
            desc = entry.get("description", "") if entry else ""
            version = entry.get("version", "?") if entry else "?"
            print(f"{name} v{version} - {desc}")
        return 0



class ClockInterpreter:
    VERSION = "0.5.0"

    def __init__(self, source: str, filename: str = "<memory>", *, debug: bool = False, unsleepy: bool = False, autoinstall: bool = False):
        self.filename = filename
        self.base_dir = Path(filename).resolve().parent if filename not in {"<memory>", ""} else Path.cwd()
        self.debug = debug
        self.unsleepy = unsleepy
        self.autoinstall = autoinstall
        self.lines = source.splitlines()
        self.functions: dict[str, FunctionDef] = {}
        self.classes: dict[str, ClockClass] = {}
        self.globals: dict[str, Any] = {}
        self.frames: list[Frame] = []
        self.pc = 0
        self.current_pc: Optional[int] = None
        self.start_time = time.perf_counter()
        self.system = ClockSystem(self)
        self.history = HistoryController(self)
        self.modules: dict[str, ModuleProxy] = {}
        self.if_pairs: dict[int, dict[str, Any]] = {}
        self.block_skip_end: dict[int, int] = {}
        self._scan_program()
        self.globals["CLASSES"] = ClassNamespace(self)
        self.builtin_modules = {
            "math": BuiltinModules.math(),
            "str": BuiltinModules.str_module(),
        }
        # `sleep` and `die` are now ordinary built-in functions.
        self.globals["sleep"] = self.system.sleep
        self.globals["die"] = self.system.die

    def resolve_path(self, raw_path: str) -> Path:
        p = Path(raw_path).expanduser()
        if p.is_absolute():
            return p
        return (self.base_dir / p).resolve()

    def _load_package_source(self, name: str) -> tuple[Path, str]:
        candidates: list[Path] = []
        for root in (self.base_dir / "ClockPacks", Path.cwd() / "ClockPacks"):
            candidates.extend([
                root / name / "main.clk",
                root / name / "index.clk",
                root / name / f"{name}.clk",
                root / f"{name}.clk",
            ])
            folder = root / name
            if folder.is_dir():
                candidates.extend(sorted(folder.glob("*.clk")))
        for root in (self.base_dir, Path.cwd()):
            candidates.extend([root / name, root / f"{name}.clk"])
        seen: set[Path] = set()
        for candidate in candidates:
            try:
                candidate = candidate.resolve()
            except OSError:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_file() and candidate.suffix.lower() == ".clk":
                try:
                    return candidate, candidate.read_text(encoding="utf-8")
                except OSError as exc:
                    raise ClockError(f"could not read package {name}: {exc}") from exc
        raise ClockError(f"module/package not found: {name}. Install with ClockInstall {name}")

    def import_module(self, name: str, alias: str) -> ModuleProxy:
        key = name.lower()
        if key in self.builtin_modules:
            module = self.builtin_modules[key]
        else:
            try:
                path, source = self._load_package_source(name)
            except ClockError:
                if not self.autoinstall:
                    raise
                print(f"Clock: dependency '{name}' missing; installing with ClockInstall...")
                manager = PackageManager(self.base_dir, realtime=False)
                result = manager.install(name)
                if result != 0:
                    raise ClockError(f"could not install dependency {name}")
                path, source = self._load_package_source(name)

            # Native packages are still implemented by main.py. The package is
            # installable, but its system calls remain centralized here.
            manifest_path = path.parent / "package.json"
            native_name = ""
            if manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    native_name = str(manifest.get("native", ""))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ClockError(f"invalid manifest at {manifest_path}: {exc}") from exc

            if native_name.lower() == "fs":
                module = BuiltinModules.fs(self)
            else:
                child = ClockInterpreter(
                    source, str(path), debug=self.debug,
                    unsleepy=self.unsleepy, autoinstall=self.autoinstall
                )
                child.run()
                exports: dict[str, Any] = {}
                exports.update({k: v for k, v in child.globals.items() if not k.startswith("__") and k != "CLASSES"})
                for fname in child.functions:
                    exports[fname] = FunctionProxy(child, fname)
                for cname, cdef in child.classes.items():
                    exports[cname] = ClassProxy(child, cdef)
                module = ModuleProxy(name, exports, str(path))
        self.modules[alias] = module
        self.globals[alias] = module
        return module


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
                        raise ClockError(f"endfunc has no matching method at line {i+1}")
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
                    raise ClockError(f"{line} without if at line {i+1}")
                # nothing else needed here; branch chains are resolved dynamically.
            elif low == "endif":
                if not istack:
                    raise ClockError(f"endif without if at line {i+1}")
                start = istack.pop()
                self.if_pairs[start] = {"end": i}
                # All branch markers are mapped later.
        if istack:
            raise ClockError("an if has no endif")

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
            raise ClockError("`me` only exists inside a method")
        if frame and name in frame.locals:
            return frame.locals[name]
        if name in self.globals:
            return self.globals[name]
        if name in self.functions:
            return FunctionProxy(self, name)
        if name in self.classes or name in {"std", "HISTORY", "history"}:
            if name == "std":
                return StdClassProxy(self)
            if name.lower() == "history":
                return self.history
        raise ClockError(f"name not found: {name}")

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
            raise ClockError("reference `\\...\\` is not closed")

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

    def _expand_variable_refs(self, expr: str) -> str:
        """Expand Clock variable references such as `$name` to evaluator names."""
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
                if i + 1 >= len(expr) or not (expr[i + 1].isalpha() or expr[i + 1] == "_"):
                    raise ClockError("`$` must be followed by a variable name")
                j = i + 1
                while j < len(expr) and (expr[j].isalnum() or expr[j] == "_"):
                    j += 1
                out.append(expr[i + 1:j])
                i = j
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    def _expand_current_line(self, expr: str) -> str:
        """Expand `@` to the exact current source line number."""
        current = str((self.current_pc + 1) if self.current_pc is not None else 0)
        out: list[str] = []
        in_string = False
        quote = ""
        for ch in expr:
            if ch in {"\"", "'"}:
                if in_string and ch == quote:
                    in_string = False
                elif not in_string:
                    in_string = True
                    quote = ch
                out.append(ch)
                continue
            if not in_string and ch == "@":
                out.append(current)
            else:
                out.append(ch)
        return "".join(out)

    def eval_expr(self, expr: str) -> Any:
        expanded = self._expand_refs(expr)  # legacy syntax remains accepted.
        expanded = self._expand_variable_refs(expanded)
        expanded = self._expand_current_line(expanded)
        expanded = expanded.replace(" xor ", " ^ ")
        expanded = expanded.replace(" XOR ", " ^ ")
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
            raise ClockError(f"function not found: {name}")
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
            raise ClockError(f"method not found: {obj.class_def.name}.{name}")
        frame = Frame(callable_name=f"me.{name}", args=args, locals={}, me=obj)
        self.frames.append(frame)
        try:
            return self._execute_range(method.start, method.end, is_function=True)
        finally:
            self.frames.pop()

    def _set_var(self, target: str, value: Any, force_global: bool = False) -> None:
        target = target.strip()
        if isinstance(value, HistoryEntry):
            value = str(value)
        if target.startswith("\\") and target.endswith("\\"):
            target = target[1:-1].strip()
        if target.startswith("me."):
            frame = self.current_frame
            if not frame or frame.me is None:
                raise ClockError("`me` only exists inside a method")
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
            raise ClockError(f"invalid assignment target: {target}")
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
        raise ClockError(f"could not assign: {target}")

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
                return self._execute_single_command_body(pc, do_command, if_stack, end_limit)
            return pc + 1, None, False

        # `... if condition` and `... ifnt condition` single-line form.
        command_part, cond_expr, invert = self._split_inline_if(line)
        if cond_expr is not None:
            cond = bool(self.eval_expr(cond_expr))
            if invert:
                cond = not cond
            return (self._execute_single_command_body(pc, command_part, if_stack, end_limit) if cond else (pc + 1, None, False))

        if low.startswith("ifnt ") or low.startswith("if "):
            invert = low.startswith("ifnt ")
            expr = line[5:].strip() if invert else line[3:].strip()
            cond = bool(self.eval_expr(expr))
            if invert:
                cond = not cond
            # Find matching branch/end for this if.
            info = self.if_pairs.get(pc)
            if info is None:
                raise ClockError(f"if without endif at line {pc+1}")
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

    def _should_record_history(self, line: str) -> bool:
        if self.history.replaying or not self.history.recording:
            return False
        low = line.strip().lower()
        # History control/access commands are metadata, not entries. This keeps
        # indexes stable and prevents replay recursion.
        compact = low.replace(" ", "")
        if compact.startswith("getclasses.history[") or compact.startswith("getclasses.history.start") or compact.startswith("getclasses.history.pause"):
            return False
        if compact.startswith("gethistory[") or compact.startswith("gethistory.start") or compact.startswith("gethistory.pause"):
            return False
        return True

    def _execute_single_command(self, pc: int, line: str, if_stack: list[dict[str, Any]], end_limit: int) -> tuple[int, Any, bool]:
        line = line.strip()
        record = self.history.add(line) if self._should_record_history(line) else None
        self.history.push_record(record)
        try:
            return self._execute_single_command_body(pc, line, if_stack, end_limit)
        finally:
            self.history.pop_record()

    def _execute_single_command_body(self, pc: int, line: str, if_stack: list[dict[str, Any]], end_limit: int) -> tuple[int, Any, bool]:
        line = line.strip()
        low = line.lower()
        if not line:
            return pc + 1, None, False

        if low.startswith("import "):
            body = line[7:].strip()
            import re
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)$", body, flags=re.IGNORECASE)
            if not m:
                raise ClockError(f"invalid import at line {pc+1}; use: import Package as name")
            package, alias = m.groups()
            self.import_module(package, alias)
            return pc + 1, None, False

        if low.startswith("set "):
            body = line[4:].strip()
            # split only the first ` as `
            pos = body.lower().find(" as ")
            if pos < 0:
                raise ClockError(f"set without `as` na linha {pc+1}")
            target = body[:pos].strip()
            expr = body[pos + 4 :].strip()
            value = self.eval_expr(expr)
            self._set_var(target, value)
            return pc + 1, None, False

        if low.startswith("global "):
            body = line[7:].strip()
            pos = body.lower().find(" as ")
            if pos < 0:
                raise ClockError(f"global without `as` na linha {pc+1}")
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
                raise ClockError(f"toline requires a number at line {pc+1}")
            if dest < 1 or dest > len(self.cleaned):
                raise ClockError(f"toline outside file: {dest}")
            if_stack.clear()
            return dest - 1, None, False

        if low.startswith("retline "):
            parts = line[8:].strip()
            try:
                amount = int(parts)
            except ValueError:
                raise ClockError(f"retline requires a number at line {pc+1}")
            if_stack.clear()
            return max(0, pc - amount), None, False

        if low.startswith("jumpline "):
            parts = line[9:].strip()
            try:
                amount = int(parts)
            except ValueError:
                raise ClockError(f"jumpline requires a number at line {pc+1}")
            if_stack.clear()
            return min(end_limit, pc + amount + 1), None, False

        if low.startswith("execline "):
            parts = line[9:].strip()
            try:
                dest = int(parts)
            except ValueError:
                raise ClockError(f"execline requires a number at line {pc+1}")
            if dest < 1 or dest > len(self.cleaned):
                raise ClockError(f"execline outside file: {dest}")
            # One-shot execution of yesple commands. Ignore any jump returned by it.
            saved_stack = list(if_stack)
            saved_pc = self.current_pc
            if_stack.clear()
            try:
                self.current_pc = dest - 1
                _, value, returned = self._execute_line(dest - 1, self.cleaned[dest - 1], if_stack, end_limit)
            finally:
                self.current_pc = saved_pc
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
            raise ClockError(f"line {pc+1}: unknown or invalid command: {line} ({exc})") from exc

    def _consume_get(self, value: Any) -> None:
        if isinstance(value, HistoryEntry):
            self.history.replay(value)
            return
        if isinstance(value, SpecialValue):
            if value.kind == "silent":
                return
            if value.kind == "history_start":
                self.history.start()
                return
            if value.kind == "history_pause":
                self.history.pause()
                return
            if value.kind == "print":
                text = self._format_value(value.value)
                print(text)
                self.history.record_output(text)
            elif value.kind == "color":
                text = value.value
                print(text)
                self.history.record_output(text)
            elif value.kind == "error":
                text = self._format_value(value.value)
                print(text, file=sys.stderr)
                self.history.record_output(f"[stderr] {text}")
            elif value.kind == "debug":
                text = f"[DEBUG] {self._format_value(value.value)}"
                print(text)
                self.history.record_output(text)
            else:
                text = self._format_value(value)
                print(text)
                self.history.record_output(text)
        else:
            text = self._format_value(value)
            print(text)
            self.history.record_output(text)

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
            raise ClockError(f"program terminated: {exc.message}") from exc
        except ClockError as exc:
            raise ClockError(f"{self.filename}: {exc}") from exc


def clock_usage() -> None:
    print(f"Clock {ClockInterpreter.VERSION}")
    print("Usage: Clock [flags] file.clk")
    print("  -v, --version     version")
    print("  -h, --help        flag help")
    print("  -H, --history     shows each command + its output")
    print("  -u, --unsleepy    ignores sleep()")
    print("  -a, --autoinstall automatically installs missing dependencies")


def clock_parse(argv: list[str]) -> tuple[dict[str, bool], str | None]:
    flags = {"version": False, "help": False, "history": False, "unsleepy": False, "autoinstall": False}
    path = None
    short_map = {"v": "version", "h": "help", "H": "history", "u": "unsleepy", "a": "autoinstall"}
    long_map = {"--version": "version", "--help": "help", "--history": "history", "--unsleepy": "unsleepy", "--autoinstall": "autoinstall"}
    for arg in argv[1:]:
        if arg in long_map:
            flags[long_map[arg]] = True
        elif arg.startswith("-") and arg != "-":
            if arg.startswith("--"):
                raise ValueError(f"bandeira desconhecida: {arg}")
            for ch in arg[1:]:
                if ch not in short_map:
                    raise ValueError(f"bandeira desconhecida: -{ch}")
                flags[short_map[ch]] = True
        else:
            if path is not None:
                raise ValueError("more than one file was provided")
            path = arg
    return flags, path


def clock_main(argv: list[str]) -> int:
    try:
        flags, path = clock_parse(argv)
    except ValueError as exc:
        print(f"Clock: {exc}", file=sys.stderr)
        clock_usage()
        return 1
    if flags["help"]:
        clock_usage()
        return 0
    if flags["version"]:
        print(ClockInterpreter.VERSION)
        return 0
    if path is None:
        clock_usage()
        return 1
    source_path = Path(path)
    if not source_path.exists():
        print(f"Clock: file not found: {source_path}", file=sys.stderr)
        return 1
    if source_path.suffix.lower() != ".clk":
        print(f"Clock: warning: {source_path.name!r} does not end with .clk", file=sys.stderr)
    try:
        source = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Clock: could not read the file: {exc}", file=sys.stderr)
        return 1
    interp = ClockInterpreter(source, str(source_path), unsleepy=flags["unsleepy"], autoinstall=flags["autoinstall"])
    rc = 0
    try:
        interp.run()
    except ClockError as exc:
        print(f"Clock: error: {exc}", file=sys.stderr)
        rc = 1
    finally:
        if flags["history"]:
            print("\n=== HISTORY ===")
            print(interp.history.formatted())
    return rc


def install_usage() -> None:
    print(f"ClockInstall {PackageManager.VERSION}")
    print("Usage: ClockInstall [flags] command [package|query]")
    print("Commands: install, uninstall, list, checkConn, searchPack")
    print("Flags:")
    print("  -v, --version      version")
    print("  -h, --help         help")
    print("  -r, --realtime     shows installation progress")
    print("  -d, --diagnostic   diagnoses a package")
    print("  -i, --info         package information")
    print()
    print("Ex.: ClockInstall install Fs")
    print("     ClockInstall -r install EasyTime")
    print("     ClockInstall -d Fs")
    print("     ClockInstall -i EasyTime")


def install_main(argv: list[str]) -> int:
    realtime = False
    args: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in {"-v", "--version"}:
            print(PackageManager.VERSION)
            return 0
        if arg in {"-h", "--help"}:
            install_usage()
            return 0
        if arg in {"-r", "--realtime"}:
            realtime = True
        elif arg in {"-d", "--diagnostic"}:
            if i + 1 >= len(argv):
                print("ClockInstall: -d requires a package", file=sys.stderr)
                return 1
            manager = PackageManager(Path.cwd(), realtime=realtime)
            return manager.diagnostic(argv[i + 1])
        elif arg in {"-i", "--info"}:
            if i + 1 >= len(argv):
                print("ClockInstall: -i requires a package", file=sys.stderr)
                return 1
            manager = PackageManager(Path.cwd(), realtime=realtime)
            return manager.info(argv[i + 1])
        else:
            args.append(arg)
        i += 1
    if not args:
        install_usage()
        return 1
    command = args[0].lower()
    manager = PackageManager(Path.cwd(), realtime=realtime)
    if command == "install" and len(args) == 2:
        return manager.install(args[1])
    if command in {"uninstall", "remove"} and len(args) == 2:
        return manager.uninstall(args[1])
    if command == "list" and len(args) == 1:
        return manager.list_installed()
    if command.lower() == "checkconn" and len(args) == 1:
        ok, message = manager.check_connection()
        print(("OK: " if ok else "ERROR: ") + message)
        return 0 if ok else 1
    if command.lower() == "searchpack" and len(args) >= 2:
        return manager.search(" ".join(args[1:]))
    print("ClockInstall: invalid command or arguments", file=sys.stderr)
    install_usage()
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    name = Path(argv[0]).name.lower()
    if name in {"clockinstall", "clockinst"}:
        return install_main(argv)
    # Explicit internal modes are also useful for wrappers and testing.
    if len(argv) >= 2 and argv[1] in {"__clockinstall__", "--clockinstall"}:
        return install_main([argv[0]] + argv[2:])
    return clock_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
