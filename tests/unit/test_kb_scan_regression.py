"""Knowledge scans retain hashes instead of repeatedly embedding unchanged files."""

from src.memory.embedder import HashEmbedder
from src.memory.kb import KnowledgeBase, file_hash
from src.memory.memory import InMemoryStore, Memory


class CountingEmbedder(HashEmbedder):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def embed(self, text):
        self.calls += 1
        return super().embed(text)


def test_knowledge_scan_retains_hashes_and_skips_unchanged_files(tmp_path):
    embedder = CountingEmbedder()
    memory = Memory(tmp_path, embedder=embedder, store=InMemoryStore())
    kb = KnowledgeBase(memory)
    menu = kb.path / "menu.txt"
    recipe = kb.path / "recipe.txt"
    menu.write_text("Japanese and Western dinner menu", encoding="utf-8")
    recipe.write_text("Steam rice before serving", encoding="utf-8")

    assert kb.scan() == {"ingested_chunks": 2, "files": 2}
    assert kb._manifest() == {"menu.txt": file_hash(menu), "recipe.txt": file_hash(recipe)}
    original = {point.id for point in memory.iter_points("knowledge")}
    calls = embedder.calls

    assert kb.scan() == {"ingested_chunks": 0, "files": 2}
    assert embedder.calls == calls
    assert {point.id for point in memory.iter_points("knowledge")} == original


def test_knowledge_scan_updates_changed_hash_and_prunes_deleted_files(tmp_path):
    memory = Memory(tmp_path, embedder=HashEmbedder(), store=InMemoryStore())
    kb = KnowledgeBase(memory)
    menu = kb.path / "menu.txt"
    recipe = kb.path / "recipe.txt"
    menu.write_text("Original dinner menu", encoding="utf-8")
    recipe.write_text("Recipe to remove", encoding="utf-8")
    kb.scan()

    menu.write_text("Updated gluten free dinner menu", encoding="utf-8")
    recipe.unlink()
    assert kb.scan() == {"ingested_chunks": 1, "files": 1}
    assert kb._manifest() == {"menu.txt": file_hash(menu)}
    points = list(memory.iter_points("knowledge"))
    assert len(points) == 1
    assert points[0].text == "Updated gluten free dinner menu"
    assert kb.scan() == {"ingested_chunks": 0, "files": 1}
