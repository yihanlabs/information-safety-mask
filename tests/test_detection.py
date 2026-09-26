from conftest import line

from safety_mask.detection import detect, normalized
from safety_mask.models import CustomField, Entity, RuleSet


def masks(lines, categories=None, custom=None, entities=None):
    return detect(
        lines, entities or [], RuleSet(categories=categories or [], custom_fields=custom or []), 2000, 2000
    )


def test_categories_are_independent_and_disabled_means_no_masks():
    item = line("姓名：张三 电话：13800138000")
    assert masks([item]) == []
    name = masks([item], ["person"])
    phone = masks([item], ["phone"])
    assert len(name) == len(phone) == 1
    assert name[0].box.x + name[0].box.width < phone[0].box.x
    assert not name[0].fallback


def test_phone_formats_and_fullwidth_spaces():
    for text in [
        "138 0013 8000",
        "+86 13800138000",
        "010-12345678",
        "（010）12345678",
        "１３８００１３８０００",
    ]:
        assert masks([line(text)], ["phone"]), text
    assert not masks([line("12345678901234567890")], ["phone"])


def test_id_and_labeled_suspected_ocr_error():
    for text in ["11010519491231002X", "护照号：E12345678", "身份证号：11010519491231OO2X"]:
        assert masks([line(text)], ["identity"]), text


def test_city_alone_not_detailed_address():
    assert (
        masks([line("北京市")], ["address"], entities=[Entity(line_id="a", start=0, end=3, label="GPE")])
        == []
    )
    assert masks([line("北京市海淀区示例路18号302室")], ["address"])


def test_custom_multiselect_normalization_repeated_and_punctuation():
    custom = [CustomField(id="one", value="张三"), CustomField(id="two", value="账号-A", enabled=False)]
    found = masks([line("张 三和张三 账号-A")], custom=custom)
    assert len(found) == 2
    assert masks([line("账号A")], custom=[CustomField(id="x", value="账号-A")]) == []
    assert masks([line("ＡＢＣ")], custom=[CustomField(id="x", value="ABC")])
    assert normalized("Ａ 张 三") == ("A张三", [0, 2, 4])


def test_custom_across_neighbor_lines():
    found = masks(
        [line("敏感内", "a", 20), line("容测试", "b", 50)], custom=[CustomField(id="x", value="敏感内容")]
    )
    assert len(found) == 2
    assert (
        masks(
            [line("敏感内", "a", 20), line("容测试", "b", 1000)],
            custom=[CustomField(id="x", value="敏感内容")],
        )
        == []
    )


def test_whole_line_fallback_is_explicit():
    item = line("姓名：张三 电话：13800138000", words=False)
    found = masks([item], ["person"])
    assert found[0].fallback
    assert found[0].box.width >= item.box.width


def test_duplicate_rules_merge_and_keep_stable_id():
    item = line("姓名：张三")
    a = masks([item], ["person"])
    b = masks(
        [item],
        ["person"],
        [CustomField(id="c", value="张三")],
        [Entity(line_id="a", start=3, end=5, label="PERSON")],
    )
    assert len(b) == 1
    assert a[0].id == b[0].id
    assert len(b[0].reasons) == 3


def test_neighboring_label_value():
    found = masks([line("姓名：", "label", 20), line("李四", "value", 20, x=100)], ["person"])
    assert len(found) == 1
    assert found[0].box.x > 90


def test_split_phone_number_and_address_continuation():
    found = masks([line("1380013", "a", 20), line("8000", "b", 50)], ["phone"])
    assert len(found) == 2
    found = masks([line("北京市示例路18号", "a", 20), line("3栋2单元302室", "b", 50)], ["address"])
    assert len(found) == 2


def test_full_value_uses_detection_bounds_not_narrow_word_bounds():
    from safety_mask.detection import span_box
    from safety_mask.models import Rect, Word

    item = line("ALPHA-42")
    item.words = [Word(start=0, end=8, box=Rect(x=28, y=24, width=60, height=14))]
    box, fallback = span_box(item, 0, 8)
    assert box == item.box
    assert fallback is False


def test_downscaled_long_image_covers_recorded_phone_edge_pixels():
    import math

    from safety_mask.models import OcrLine, Rect, Word

    # Recorded geometry from the synthetic 1800 x 9000 / 320-line regression.
    item = OcrLine(
        id="phone",
        text="电话：13800138000",
        box=Rect(x=938.25, y=2749.5, width=308.25, height=31.5),
        words=[Word(start=3, end=14, box=Rect(x=1060.25, y=2749.5, width=180, height=31.5))],
    )
    found = detect([item], [], RuleSet(categories=["phone"]), 1800, 9000)
    assert len(found) == 1
    box = found[0].box
    for x, y in [(1053, 2759), (1053, 2760), (1053, 2761)]:
        assert math.floor(box.x) <= x < math.ceil(box.x + box.width)
        assert math.floor(box.y) <= y < math.ceil(box.y + box.height)
    # The label remains visible; the fix is not whole-row masking.
    assert box.x > 1012.25
