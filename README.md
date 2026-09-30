# Clock

[![Version](https://img.shields.io/badge/version-0.2.0-blue.svg)](https://github.com)
[![Language](https://img.shields.io/badge/language-Clock-.clk-orange.svg)](https://github.com)
[![Python](https://img.shields.io/badge/python-3.9+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com)
[![Open Source](https://img.shields.io/badge/open%20source-community-success.svg)](https://github.com)
[![Status](https://img.shields.io/badge/status-early%20preview-yellow.svg)](https://github.com)

**Clock** is a small, line-oriented, object-oriented programming language. Programs live in `.clk` files and run on a Python interpreter.

This project is offered to the community so people can use it, fork it, fix it, and make it better.

---

## Why Clock exists

Clock is an experiment in a readable, line-by-line language:

- one command per line
- explicit `set` / `get` / `return`
- classes and methods with `me`
- a tiny system object (`$`) for time, color, sleep, and program control
- jumps by line number when you want a very direct control flow

It is **not** trying to replace Python, JavaScript, or any production language. It is a playground: easy to read, easy to extend, and small enough that a new contributor can understand the whole interpreter.

If you enjoy language design, interpreters, or unusual syntax, you are invited to improve Clock.

---

## What you need

- Python 3.9 or newer
- The interpreter file, usually named `clock.py`
- Optional launcher script named `Clock` that runs `clock.py`

No extra packages are required. The standard library is enough.

---

## Install and run

Clone or copy the project, then run a source file:

```bash
python3 clock.py program.clk
```

If you have the launcher script:

```bash
python3 Clock program.clk
# or, after chmod +x Clock
./Clock program.clk
```

Help and version:

```bash
python3 clock.py --help
python3 clock.py --version
```

The interpreter expects `.clk` files. Other extensions still run, but you get a warning.

---

## Hello, Clock

Save this as `hello.clk`:

```clk
get CLASSES.std().print("Hello, Clock")
```

Run:

```bash
python3 clock.py hello.clk
```

A slightly richer example:

```clk
set name as "Ada"
set n as 3

get CLASSES.std().print("Hello, " + \name\)
get CLASSES.std().print("2 + 2 = " + \2 + 2\)

if \n\ > 2
    get CLASSES.std().print("n is greater than 2")
else
    get CLASSES.std().print("n is small")
endif
```

---

## How Clock programs are written

Clock is **line-oriented**. Each non-empty line is a command, a block marker, or an expression. `#` starts a comment until the end of the line (outside of strings).

### Values

| Clock | Meaning |
| --- | --- |
| `true` / `false` | booleans |
| `null` | nothing |
| `1`, `3.14` | numbers |
| `"text"` or `'text'` | strings |
| `[1, 2, 3]` | list |
| `{"a": 1}` | dictionary |

Printed booleans and null appear as `true`, `false`, and `null`.

### Assignment

```clk
set x as 10
set msg as "hi"
set items as [1, 2, 3]
global counter as 0
```

- `set` writes to the current function/method scope, or to globals at top level
- `global` always writes to the program globals
- You can assign into fields and indexes: `set obj.field as 1`, `set arr[0] as 9`, `set me.score as 100`

### Output

```clk
get x
get "hello"
get CLASSES.std().print("normal output")
get CLASSES.std().erro("goes to stderr")
get CLASSES.std().debug("debug line")
```

`get` evaluates an expression and prints it. Special values from `std` and `$` choose how that print looks.

### Expressions and `\references\`

Clock expressions are a restricted Python-like subset: arithmetic, comparisons, `and` / `or` / `not`, lists, dicts, indexing, attributes, and calls.

Wrap Clock names and nested expressions in backslashes when you interpolate them into a larger expression:

```clk
set a as 5
set b as 7
get \a\ + \b\
get "sum = " + \a + b\
```

`xor` is supported and means boolean exclusive-or:

```clk
get true xor false
```

### Conditions

Block form:

```clk
if \x\ > 0
    get "positive"
elseif \x\ == 0
    get "zero"
else
    get "negative"
endif
```

Inverted test:

```clk
ifnt \ready\
    get "not ready"
endif
```

Single-line forms:

```clk
get "ok" if \x\ > 0
get "missing" ifnt \ready\
if \x\ > 0 do get "ok"
ifnt \ready\ do get "missing"
```

### Functions

```clk
func greet
    set who as me.args[0]
    return "Hello, " + \who\
endfunc

get greet("Clock")
```

Inside a running function, `name.args` is the argument list. `return` leaves the function. A function without `return` yields the last evaluated value.

### Classes and methods

```clk
class Counter
    me.init
        set me.value as 0
    endfunc me.init

    me.add
        set me.value as \me.value\ + \me.args[0]\
        return \me.value\
    endfunc me.add
end Counter

set c as CLASSES.Counter()
c.init()
get c.add(5)
get c.add(2)
```

Rules:

- Declare a class with `class Name` and close it with `end Name`
- Methods are `me.method` … `endfunc me.method` (or `func me.method`)
- Inside a method, `me` is the current object
- Create instances with `CLASSES.ClassName(args)`
- Constructor arguments are available on the object as `me.args` during construction-related code; store what you need in fields with `set me.field as ...`

### Jumps (use sparingly)

These exist because Clock is line-oriented. They are powerful and easy to abuse.

| Command | Effect |
| --- | --- |
| `toline 12` | jump to source line 12 |
| `jumpline 3` | skip forward 3 lines |
| `retline 2` | go back 2 lines |
| `execline 8` | run line 8 once, then continue |

Prefer `if` / functions when you can. Line jumps are best for tiny scripts and experiments.

---

## The system object: `$`

`$` is Clock’s runtime. In expressions it becomes the internal name `clock`.

### Time and date

```clk
get $.time          # milliseconds since the program started
get $.line          # current source line (1-based)
get $.date.day
get $.date.year
get $.date.hour
get $.date.minute
get $.date.second
get $.date.weekday
```

Useful aliases:

| Field | Alias |
| --- | --- |
| `day` | `d` |
| `year` | `y` |
| `moth` | `mth` *(month, 01–12)* |
| `hour` | `h` |
| `minute` | `min` |
| `second` | `s` |
| `milisseconds` | `ms` |
| `weekday` | `wd` |

Weekday names are currently in Portuguese (`segunda-feira`, `terça-feira`, …). Date fields are formatted strings and can be called with `()`.

```clk
get $.sleep(250)    # pause 250 milliseconds
```

### Color and stop

```clk
get $.color("green text", "green")
get $.color("warning", "yellow")
```

Named colors: `black`, `red`, `green`, `yellow`, `blue`, `magenta`, `cyan`, `white`, `gray` / `grey`, `bright-black`, `bright-red`, `bright-green`, `bright-yellow`, `bright-blue`, `bright-magenta`, `bright-cyan`, `bright-white`, `reset`.

```clk
$.die("something went wrong")
```

`$.die` stops the program.

---

## Built-in classes

### `CLASSES.std`

```clk
set io as CLASSES.std()
get io.print("hello")
get io.erro("problem")
get io.debug("trace")
```

### `CLASSES.exec`

```clk
set sh as CLASSES.exec()
get sh.execute("echo Clock")
```

`execute` runs a shell command and returns stdout. A non-zero exit is an error.

Treat this as a sharp tool. Do not pass untrusted strings into the shell.

### Your classes

User classes registered while the file is scanned are available as `CLASSES.YourName`.

---

## Example: a tiny clock

```clk
set io as CLASSES.std()
set d as $.date

get io.print("Clock " + "0.2.0")
get io.print("Today is " + \d.weekday\ + ", " + \d.year\ + "-" + \d.moth\ + "-" + \d.day\)
get io.print("Local time " + \d.hour\ + ":" + \d.minute\ + ":" + \d.second\)
get $.color("running for " + \$.time\ + " ms", "cyan")
```

---

## Example: objects

```clk
class Greeter
    me.hello
        set target as "world"
        set target as \me.args[0]\ if \len(me.args)\ > 0
        return "Hello, " + \target\
    endfunc me.hello
end Greeter

set g as CLASSES.Greeter()
get g.hello()
get g.hello("community")
```

`len` works because Clock evaluates a safe subset of Python expressions and exposes ordinary sequence operations.

---

## Interpreter notes

Current interpreter version: **0.2.0**.

What the evaluator allows:

- literals: string, int, float, bool, null
- `+ - * / // % **`
- comparisons: `== != > >= < <=`
- `and`, `or`, `not`, `xor`
- lists, tuples, dicts, indexing, slices
- attribute access and calls
- no keyword arguments in calls yet

Error messages from the bundled interpreter are currently in Portuguese. English messages would be a welcome contribution.

Known quirks you may want to clean up:

- month is spelled `moth` / `mth` in `$.date`
- milliseconds is spelled `milisseconds`
- `CLASSES.exec().execute` uses `shell=True`
- line-number jumps clear the `if` stack

---

## Project layout

A typical tree:

```text
.
├── clock.py      # interpreter
├── Clock         # optional launcher
├── README.md
└── examples/
    └── hello.clk
```

Suggested launcher (`Clock`):

```python
#!/usr/bin/env python3
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.argv[0] = "Clock"
runpy.run_path(str(HERE / "clock.py"), run_name="__main__")
```

---

## This is a community project

Clock is published so other people can learn from it and improve it.

You are welcome to:

- add examples and a test suite
- document edge cases
- translate runtime errors to English (or make the language configurable)
- tighten the parser and the `\reference\` expander
- add loops (`while` / `for`) instead of relying on `toline`
- add modules / `use` / file imports
- fix `$.date` field names
- sandbox or replace `exec`
- build syntax highlighting for editors
- experiment with a new backend

If you publish a fork, keep the spirit: small language, readable programs, easy to hack on.

A good first contribution is a folder of `.clk` examples that exercise functions, classes, conditions, and `$`.

---

## Contributing

1. Fork or copy the repository.
2. Keep changes small and described in plain language.
3. Add a `.clk` example that shows the new behavior.
4. Do not silently break existing scripts if you can avoid it.
5. Open a pull request or share a patch with notes.

Suggested extras for a healthier project:

- pick a license (MIT is a simple default for community tools)
- add `examples/` and `tests/`
- record breaking changes in a short `CHANGELOG.md`

---

## License

Choose and add a license file before you publish widely. Until you do, treat the code as “shared for the community to study and improve,” and ask before using it in a commercial product.

---

## Status

Clock 0.2.0 is an early preview. The language is usable for short scripts and for learning how interpreters work. Expect sharp edges. That is part of the invitation: help turn this into something clearer, safer, and more fun to write.
