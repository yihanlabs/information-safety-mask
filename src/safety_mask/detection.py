from __future__ import annotations

import hashlib
import re
import unicodedata

from .models import CATEGORY_LABELS, Entity, Mask, OcrLine, Rect, RuleSet


def normalized(text: str) -> tuple[str, list[int]]:
    chars, positions = [], []
    for index, char in enumerate(text):
        for converted in unicodedata.normalize("NFKC", char):
            if not converted.isspace():
                chars.append(converted)
                positions.append(index)
    return "".join(chars), positions


LABEL = re.compile(
    r"详细住址|详细地址|身份证号码|身份证号|证件号码|证件号|护照号码|护照号|电话号码|手机号码|联系电话|收件人|联系人|客户姓名|姓名|户名|住址|地址|手机|电话"
)
PERSON_LABEL = re.compile(r"(?:客户姓名|姓名|联系人|收件人|户名)[:：]?")
ADDRESS_LABEL = re.compile(r"(?:详细住址|详细地址|住址|地址)[:：]?")
ID_LABEL = re.compile(r"(?:身份证号码|身份证号|证件号码|证件号|护照号码|护照号)[:：]?")
PHONE = re.compile(
    r"(?<![0-9])(?:\+?86[-]?)?1[3-9][0-9]{9}(?![0-9])|(?<![0-9])(?:\+?86[-]?)?(?:\(0[0-9]{2,3}\)|0[0-9]{2,3})[-]?[0-9]{7,8}(?:[-转][0-9]{1,6})?(?![0-9])"
)
IDENTITY = re.compile(
    r"(?<![A-Za-z0-9])(?:[1-8][0-9]{5}(?:19|20)[0-9]{2}[01][0-9][0-3][0-9][0-9]{3}[0-9Xx]|[1-8][0-9]{5}[0-9]{2}[01][0-9][0-3][0-9][0-9]{3}|[EG][0-9]{8}|E[A-HJ-NP-Z][0-9]{7})(?![A-Za-z0-9])"
)
DETAIL = re.compile(r"街道|大道|路|街|巷|弄|小区|社区|大厦|花园|公寓|村|镇|乡")
UNIT = re.compile(r"[0-9一二三四五六七八九十]+(?:号|栋|幢|单元|室)")
RUN = re.compile(r"[\u4e00-\u9fffA-Za-z0-9·#()\-]{5,}")


def union(rects: list[Rect]) -> Rect:
    x, y = min(r.x for r in rects), min(r.y for r in rects)
    right, bottom = max(r.x + r.width for r in rects), max(r.y + r.height for r in rects)
    return Rect(x=x, y=y, width=right - x, height=bottom - y)


def span_box(line: OcrLine, start: int, end: int) -> tuple[Rect, bool]:
    visible = [i for i, char in enumerate(line.text) if not char.isspace()]
    if visible and start <= visible[0] and end > visible[-1]:
        # Whole-line values use the detection box; CTC word alignment can trim edge strokes.
        return line.box, False
    words = [w for w in line.words if w.end > start and w.start < end]
    covered = set()
    for word in words:
        covered.update(range(max(start, word.start), min(end, word.end)))
    required = {i for i in range(start, min(end, len(line.text))) if not line.text[i].isspace()}
    if words and required.issubset(covered):
        box = union([w.box for w in words])
        x1, x2 = box.x, box.x + box.width
        if visible and start <= visible[0]:
            x1 = min(x1, line.box.x)
        if visible and end > visible[-1]:
            x2 = max(x2, line.box.x + line.box.width)
        return Rect(
            x=x1,
            y=min(box.y, line.box.y),
            width=x2 - x1,
            height=max(box.y + box.height, line.box.y + line.box.height) - min(box.y, line.box.y),
        ), False
    return line.box, True


def padded(rect: Rect, width: int, height: int) -> Rect:
    # Detector coordinates are mapped back from a canvas with a 4000px long side.
    # A fixed 16px ceiling is too small for large originals (one detector pixel
    # may represent many source pixels). Keep the existing padding for ordinary text.
    ceiling = max(16, min(128, max(width, height) / 4000 * 8))
    # Word alignment is quantized on the resized detector canvas. Account for
    # one mapped pixel in addition to stroke padding on downscaled originals.
    # The 320-line regression exposed three edge pixels without this allowance.
    scale = max(width, height) / 4000
    rounding_allowance = scale if scale > 1 else 0
    margin = max(3, min(ceiling, rect.height * 0.18 + rounding_allowance))
    x, y = max(0, rect.x - margin), max(0, rect.y - margin)
    right, bottom = min(width, rect.x + rect.width + margin), min(height, rect.y + rect.height + margin)
    return Rect(x=x, y=y, width=max(1, right - x), height=max(1, bottom - y))


def neighbors(a: OcrLine, b: OcrLine) -> bool:
    height = max(a.box.height, b.box.height)
    same_row = abs(a.box.y - b.box.y) < height * 0.6 and 0 <= b.box.x - (a.box.x + a.box.width) < height * 8
    next_row = 0 <= b.box.y - (a.box.y + a.box.height) < height * 1.8 and abs(a.box.x - b.box.x) < height * 4
    return same_row or next_row


def detect(
    lines: list[OcrLine], entities: list[Entity], rules: RuleSet, width: int, height: int
) -> list[Mask]:
    results: dict[str, Mask] = {}
    by_id = {line.id: line for line in lines}

    def add(line: OcrLine, start: int, end: int, reason: str):
        if start >= end:
            return
        rect, fallback = span_box(line, start, end)
        # Identity is geometry-based: overlapping rules do not resurrect a user exclusion.
        raw = f"{line.id}:" + ",".join(f"{v:.2f}" for v in (rect.x, rect.y, rect.width, rect.height))
        key = hashlib.sha256(raw.encode()).hexdigest()[:20]
        if key in results:
            if reason not in results[key].reasons:
                results[key].reasons.append(reason)
            return
        results[key] = Mask(
            id=key, box=padded(rect, width, height), source="auto", reasons=[reason], fallback=fallback
        )

    def add_norm(line, mapping, start, end, reason):
        if mapping and end > start and end <= len(mapping):
            add(line, mapping[start], mapping[end - 1] + 1, reason)

    for line in lines:
        text, mapping = normalized(line.text)
        if "phone" in rules.categories:
            for match in PHONE.finditer(text):
                add_norm(line, mapping, match.start(), match.end(), "电话号码 · 格式匹配")
        if "identity" in rules.categories:
            for match in IDENTITY.finditer(text):
                add_norm(line, mapping, match.start(), match.end(), "证件号码 · 格式匹配")
        for category, pattern in [
            ("person", PERSON_LABEL),
            ("identity", ID_LABEL),
            ("address", ADDRESS_LABEL),
        ]:
            if category not in rules.categories:
                continue
            for label in pattern.finditer(text):
                start, end = label.end(), len(text)
                following = LABEL.search(text, start)
                if following:
                    end = following.start()
                value = text[start:end].strip(":：,，;；")
                start += len(text[start:end]) - len(text[start:end].lstrip(":：,，;；"))
                end = start + len(value)
                if value:
                    if category == "person":
                        match = re.match(r"[\u4e00-\u9fff·]{2,12}|[A-Za-z][A-Za-z·.\-]{1,60}", value)
                        if match:
                            add_norm(line, mapping, start, start + match.end(), "人名 · 标签关联")
                    elif category == "identity":
                        match = re.match(r"[A-Za-z0-9]{6,24}", value)
                        if match:
                            add_norm(
                                line,
                                mapping,
                                start,
                                start + match.end(),
                                "证件号码 · 标签关联（含疑似识别错误）",
                            )
                    elif len(value) >= 4:
                        add_norm(line, mapping, start, end, "详细住址 · 标签关联")
                else:
                    # A label and its value frequently occupy separate OCR boxes.
                    choices = [
                        other
                        for other in lines
                        if other.id != line.id
                        and neighbors(line, other)
                        and not LABEL.match(normalized(other.text)[0])
                    ]
                    choices.sort(
                        key=lambda other: (
                            abs(other.box.y - line.box.y) * 3
                            + abs(other.box.x - (line.box.x + line.box.width))
                        )
                    )
                    if choices:
                        other = choices[0]
                        other_text, other_map = normalized(other.text)
                        next_label = LABEL.search(other_text)
                        stop = next_label.start() if next_label else len(other_text)
                        if stop:
                            add_norm(other, other_map, 0, stop, CATEGORY_LABELS[category] + " · 相邻字段")
        if "address" in rules.categories:
            for match in RUN.finditer(text):
                value = match.group()
                if DETAIL.search(value) and (UNIT.search(value) or len(value) >= 8):
                    # Do not include preceding labels or a following telephone field.
                    offsets = list(LABEL.finditer(value))
                    start, end = match.start(), match.end()
                    for label in offsets:
                        if ADDRESS_LABEL.fullmatch(label.group()):
                            start = match.start() + label.end()
                        elif match.start() + label.start() > start:
                            end = match.start() + label.start()
                            break
                    add_norm(line, mapping, start, end, "详细住址 · 街道与门牌规则")

    for entity in entities:
        if entity.label == "PERSON" and "person" in rules.categories and entity.line_id in by_id:
            line = by_id[entity.line_id]
            value = line.text[entity.start : entity.end]
            if len(value.strip()) >= 2 and not LABEL.fullmatch(value):
                add(line, entity.start, entity.end, "人名 · 本地中文实体识别")
        if (
            entity.label in {"GPE", "LOC", "FAC"}
            and "address" in rules.categories
            and entity.line_id in by_id
        ):
            line = by_id[entity.line_id]
            tail = line.text[entity.start :]
            stop = LABEL.search(tail)
            if stop and stop.start() > 0:
                tail = tail[: stop.start()]
            if DETAIL.search(tail) or UNIT.search(tail):
                add(line, entity.start, entity.start + len(tail.rstrip()), "详细住址 · 地名与详细地址")

    if "address" in rules.categories:
        address_line_ids = {
            line.id
            for line in lines
            for mask in results.values()
            if any(reason.startswith("详细住址") for reason in mask.reasons)
            and mask.box.y <= line.box.y + line.box.height / 2 <= mask.box.y + mask.box.height
        }
        for first in lines:
            if first.id not in address_line_ids:
                continue
            for other in lines:
                if other.id == first.id or other.box.y <= first.box.y or not neighbors(first, other):
                    continue
                if abs(other.box.x - first.box.x) > max(first.box.height, other.box.height) * 3:
                    continue
                text, mapping = normalized(other.text)
                if UNIT.search(text) and not LABEL.search(text):
                    add_norm(other, mapping, 0, len(text), "详细住址 · 楼栋门牌续行")

    selected = [(item, normalized(item.value)[0]) for item in rules.custom_fields if item.enabled]
    groups = [[line] for line in lines]
    ordered = sorted(lines, key=lambda item: (item.box.y, item.box.x))
    for i, first in enumerate(ordered):
        group = [first]
        for other in ordered[i + 1 : i + 3]:
            if not neighbors(group[-1], other):
                break
            group = group + [other]
            groups.append(group)
    for group in groups:
        text, positions = "", []
        for line in group:
            part, mapping = normalized(line.text)
            text += part
            positions.extend((line, index) for index in mapping)
        if len(group) > 1:
            for category, pattern in [("phone", PHONE), ("identity", IDENTITY)]:
                if category not in rules.categories:
                    continue
                for match in pattern.finditer(text):
                    matched = positions[match.start() : match.end()]
                    if len({line.id for line, _ in matched}) < 2:
                        continue
                    pieces = {}
                    for line, index in matched:
                        pieces.setdefault(line.id, (line, []))[1].append(index)
                    for line, indices in pieces.values():
                        add(
                            line,
                            min(indices),
                            max(indices) + 1,
                            CATEGORY_LABELS[category] + " · 相邻文本拼接",
                        )
        for item, needle in selected:
            if not needle:
                continue
            offset = 0
            while (found := text.find(needle, offset)) >= 0:
                pieces: dict[str, tuple[OcrLine, list[int]]] = {}
                for line, index in positions[found : found + len(needle)]:
                    pieces.setdefault(line.id, (line, []))[1].append(index)
                for line, indices in pieces.values():
                    add(line, min(indices), max(indices) + 1, "自定义字段 · " + item.value)
                offset = found + 1
    return list(results.values())
