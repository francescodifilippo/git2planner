from __future__ import annotations

from git2planner.attachments import (
    decode_reference_key,
    reference_key,
    reference_type,
    sanitize_filename,
    sanitize_folder,
)
from git2planner.markdown import (
    append_task_items,
    extract_links,
    extract_task_items,
    set_task_item_states,
    task_item_lines,
    to_plain_text,
)

BODY = """<!-- issue template comment
spanning lines -->
## Summary

The **login** button _sometimes_ fails. See [the docs](https://example.com/docs) and `code`.

- first
- [ ] todo item
- [x] done item

```python
- [ ] not a task
print("**kept**")
```

![screen](https://example.com/a.png)
<img src="https://example.com/b.png" alt="other">
Bare link https://example.com/c.
"""


def test_plain_text_conversion():
    text = to_plain_text(BODY)
    assert "issue template" not in text
    assert text.startswith("Summary")
    assert (
        "The login button sometimes fails. See the docs (https://example.com/docs) and code."
        in text
    )
    assert "• first" in text
    assert "[ ] todo item" in text
    assert 'print("**kept**")' in text
    assert "[image: screen]" in text
    assert "[image: other]" in text
    assert "```" not in text


def test_plain_text_without_task_items():
    text = to_plain_text(BODY, strip_task_items=True)
    assert "todo item" not in text
    assert "- [ ] not a task" in text


def test_task_items_skip_code_blocks():
    items = extract_task_items(BODY)
    assert [(item.title, item.checked) for item in items] == [
        ("todo item", False),
        ("done item", True),
    ]
    lines = BODY.split("\n")
    assert lines[items[0].line] == "- [ ] todo item"


def test_set_and_append_task_items():
    items = extract_task_items(BODY)
    updated = set_task_item_states(BODY, {items[0].line: True, items[1].line: False})
    assert "- [x] todo item" in updated
    assert "- [ ] done item" in updated
    assert "- [ ] not a task" in updated
    appended = append_task_items("Intro", [("new", False), ("old", True)])
    assert appended == "Intro\n\n- [ ] new\n- [x] old"
    assert append_task_items("- [ ] a", [("b", False)]) == "- [ ] a\n- [ ] b"
    assert append_task_items("", [("b", True)]) == "- [x] b"
    assert task_item_lines(BODY) == ["- [ ] todo item", "- [x] done item"]


def test_extract_links():
    links = extract_links(BODY)
    urls = [link.url for link in links]
    assert urls == [
        "https://example.com/docs",
        "https://example.com/a.png",
        "https://example.com/b.png",
        "https://example.com/c",
    ]
    assert links[1].is_image
    assert links[1].text == "screen"
    assert links[2].text == "other"


def test_relative_links_are_resolved():
    links = extract_links(
        "![x](uploads/a.png) [y](#anchor)", base_url="https://git.example.com/o/r/"
    )
    assert [link.url for link in links] == ["https://git.example.com/o/r/uploads/a.png"]


def test_reference_keys():
    url = "https://contoso.sharepoint.com/a b/file.docx#x@y:1%"
    key = reference_key(url)
    assert "." not in key
    assert ":" not in key
    assert "@" not in key
    assert "#" not in key
    assert decode_reference_key(key) == url


def test_reference_types_and_file_names():
    assert reference_type("report.PDF") == "Pdf"
    assert reference_type("budget.xlsx") == "Excel"
    assert reference_type("https://x/y/slides.pptx") == "PowerPoint"
    assert reference_type("image.png") == "Other"
    assert sanitize_filename("a:b*c?.txt") == "a_b_c_.txt"
    assert sanitize_filename("photo", content_type="image/png") == "photo.png"
    assert sanitize_filename("  .hidden. ") == "hidden"
    assert sanitize_folder("git2planner/acme:x//1") == "git2planner/acme_x/1"
