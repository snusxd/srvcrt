"""Read the comments and trailing commas commonly used in mod metadata JSON."""

from __future__ import annotations

import json


def loads(source: str):
    without_comments: list[str] = []
    quoted = False
    escaped = False
    index = 0
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if quoted:
            without_comments.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
        elif char == '"':
            quoted = True
            without_comments.append(char)
            index += 1
        elif char == "/" and next_char == "/":
            index += 2
            while index < len(source) and source[index] not in "\r\n":
                index += 1
        elif char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(source) and source[index:index + 2] != "*/":
                if source[index] in "\r\n":
                    without_comments.append(source[index])
                index += 1
            if index + 1 >= len(source):
                raise ValueError("Unclosed block comment in mod metadata")
            index += 2
        else:
            without_comments.append(char)
            index += 1

    cleaned = "".join(without_comments)
    without_trailing_commas: list[str] = []
    quoted = escaped = False
    for index, char in enumerate(cleaned):
        if quoted:
            without_trailing_commas.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            without_trailing_commas.append(char)
        elif char == ",":
            following = index + 1
            while following < len(cleaned) and cleaned[following].isspace():
                following += 1
            if following >= len(cleaned) or cleaned[following] not in "}]":
                without_trailing_commas.append(char)
        else:
            without_trailing_commas.append(char)
    return json.loads("".join(without_trailing_commas))
