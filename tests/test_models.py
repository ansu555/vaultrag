"""Step 1 verify: every core model constructs, round-trips, and derives correctly."""

from __future__ import annotations

import pytest

from vaultrag.models import (
    Block,
    BlockType,
    Chunk,
    Document,
    DocumentMetadata,
    Link,
    LinkKind,
    SearchResult,
    Snippet,
    chunk_point_id,
)


def _doc() -> Document:
    return Document(
        metadata=DocumentMetadata(
            path="projects/vaultrag/chunking-strategy.md",
            title="Chunking Strategy",
            tags=["project", "project/vaultrag"],
            aliases=["chunking"],
            frontmatter={"status": "active", "custom": {"nested": [1, 2, 3]}},
            created_ts=1_700_000_000,
            modified_ts=1_700_000_100,
            hash="blake3:deadbeef",
        ),
        blocks=[
            Block(type=BlockType.heading, text="Design", level=2, heading_path=[]),
            Block(
                type=BlockType.paragraph,
                text="Heading-tree based.",
                heading_path=["Design"],
                line_start=3,
                line_end=3,
            ),
            Block(
                type=BlockType.code,
                text="print('hi')",
                lang="python",
                heading_path=["Design"],
            ),
        ],
        links=[
            Link(kind=LinkKind.wikilink, target="chunking-heading-tree-not-fixed-size"),
            Link(kind=LinkKind.embed, target="diagram", anchor="^abc", alias="see diagram"),
        ],
    )


def _chunk() -> Chunk:
    return Chunk(
        project_id="vaultrag-design",
        doc_id="projects/vaultrag/chunking-strategy.md",
        path="projects/vaultrag/chunking-strategy.md",
        ix=0,
        text="Child chunk = contiguous blocks under the lowest heading.",
        contextual_prefix="«vaultrag-design ▸ projects/vaultrag ▸ Chunking Strategy ▸ Design»",
        heading_path=["Design"],
        anchor="design",
        tags=["project/vaultrag"],
        langs=["python"],
        has_code=True,
        chunk_hash="blake3:cafef00d",
        tokens=42,
    )


@pytest.mark.parametrize("factory", [_doc, _chunk])
def test_round_trip(factory) -> None:  # type: ignore[no-untyped-def]
    model = factory()
    for dumped in (model.model_dump(), model.model_dump(mode="json")):
        assert type(model).model_validate(dumped) == model
    assert type(model).model_validate_json(model.model_dump_json()) == model


def test_block_atomicity() -> None:
    assert Block(type=BlockType.code, text="x").is_atomic
    assert Block(type=BlockType.table, text="| a |").is_atomic
    assert Block(type=BlockType.callout, text="!note").is_atomic
    assert not Block(type=BlockType.paragraph, text="hi").is_atomic
    assert not Block(type=BlockType.heading, text="H", level=1).is_atomic


def test_chunk_embed_text_prepends_prefix() -> None:
    c = _chunk()
    assert c.embed_text.startswith(c.contextual_prefix)
    assert c.text in c.embed_text
    bare = c.model_copy(update={"contextual_prefix": ""})
    assert bare.embed_text == bare.text


def test_point_id_deterministic_and_scoped() -> None:
    c = _chunk()
    assert c.point_id == chunk_point_id(c.project_id, c.path, c.ix)
    # stable across calls, unique per (project, path, ix)
    assert chunk_point_id("p", "a.md", 0) == chunk_point_id("p", "a.md", 0)
    assert chunk_point_id("p", "a.md", 0) != chunk_point_id("p", "a.md", 1)
    assert chunk_point_id("p1", "a.md", 0) != chunk_point_id("p2", "a.md", 0)


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValueError):
        DocumentMetadata(path="a.md", title="A", bogus=1)  # type: ignore[call-arg]


def test_search_result_shape() -> None:
    res = SearchResult(
        query="how does chunking merge tiny sections",
        project_id="vaultrag-design",
        snippets=[
            Snippet(
                path="projects/vaultrag/chunking-strategy.md",
                title="Chunking Strategy",
                heading_path=["Design"],
                anchor="design",
                text="Tiny sibling sections merge until >=120 tokens.",
                score=0.91,
                tokens=12,
            )
        ],
        total_tokens=12,
    )
    assert SearchResult.model_validate(res.model_dump()) == res
    assert res.total_tokens == res.snippets[0].tokens
