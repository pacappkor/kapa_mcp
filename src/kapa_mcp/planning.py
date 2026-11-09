# SPDX-License-Identifier: GPL-3.0-only
"""Bounded ordered selection syntax and fixed, local compilation driver."""

import re


QUESTION_ID = re.compile(r"edu/([12])/([a-z][a-z0-9_]{0,63})/([1-9][0-9]{0,2})/([0-9]{2})\Z")
MAX_QUESTIONS = 100
MAX_ROUNDS = 10


def selection(question_ids):
    if type(question_ids) is not list or not 1 <= len(question_ids) <= MAX_QUESTIONS:
        raise ValueError("Select 1–100 ordered question IDs")
    rows = []
    for value in question_ids:
        match = QUESTION_ID.fullmatch(value) if isinstance(value, str) else None
        if not match or match[4] == "00":
            raise ValueError("Invalid question identifier")
        rows.append(match.groups())
    if len(set(question_ids)) != len(question_ids):
        raise ValueError("Duplicate question IDs are unsupported")
    if len({row[:2] for row in rows}) != 1:
        raise ValueError("Mixed exams or subjects are unsupported")
    if len({row[2] for row in rows}) > MAX_ROUNDS:
        raise ValueError("Select at most 10 rounds")
    return rows


def renumber(text, number, exam):
    # Only known upstream top-level question labels are transformed. Internal
    # references and source metadata stay verbatim; unsupported layouts fail.
    pattern = re.compile(r"(?m)^\\begin\{(question|longquestion)\}\{([^{}\n]*)\}")
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise ValueError("Expected one supported question environment")
    match = matches[0]
    expected = "question" if exam == 1 else "longquestion"
    label = r"[0-9]+" if exam == 1 else r"【문제\s+[0-9]+】"
    if match[1] != expected or not re.fullmatch(label, match[2]):
        raise ValueError("Unsupported question numbering format")
    replacement = str(number) if exam == 1 else f"【문제 {number}】"
    return text[:match.start(2)] + replacement + text[match.end(2):]


# Loaded only inside an owned snapshot. All paths are fixed by this adapter;
# use the reviewed upstream preflight and compile_tex without its CLI selector.
DRIVER = '''# SPDX-License-Identifier: GPL-3.0-only
import pathlib, runpy, sys
root = pathlib.Path(__file__).resolve().parent
build = runpy.run_path(str(root / 'tools' / 'build.py'))
if not build['preflight']():
    sys.exit(2)
out = root / 'build'
out.mkdir(exist_ok=True)
tex = out / 'selection.tex'
tex.write_bytes((root / 'selection.tex').read_bytes())
ok, errors, missing = build['compile_tex'](tex, out)
print('OK' if ok else 'FAIL', *errors, sep='\\n')
sys.exit(0 if ok else 1)
'''
