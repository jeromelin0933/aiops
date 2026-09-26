from pathlib import Path

import pytest
import yaml

from knowledge_index import KnowledgeConfigError, load_knowledge_config


CONFIG = Path("configs/knowledge_index.yaml")


def test_tracked_config_is_non_secret_and_fixed_to_approved_baseline() -> None:
    config = load_knowledge_config(CONFIG)
    assert config.manifest_path == "configs/knowledge_manifest.json"
    assert config.capability.provider == "google"
    assert config.capability.model == "text-embedding-004"
    assert config.capability.embedding_dimension == 768
    assert config.capability.hidden_retries_disabled
    assert config.chroma_path == "var/knowledge_index/chroma"
    assert "source_root" not in CONFIG.read_text(encoding="utf-8")


def test_provider_model_path_and_unknown_config_substitution_fail_closed(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    raw["embedding"]["model"] = "substitute-model"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(KnowledgeConfigError):
        load_knowledge_config(path)
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    raw["index"]["path"] = "../outside"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(KnowledgeConfigError):
        load_knowledge_config(path)
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    raw["api_key"] = "synthetic-secret"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(KnowledgeConfigError):
        load_knowledge_config(path)
