from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def test_authority_artifacts_are_tracked_and_fixed_to_lf() -> None:
    tracked = set(git("ls-files", "configs/knowledge_manifest.json", "docs/knowledge/*.md").splitlines())
    assert "configs/knowledge_manifest.json" in tracked
    assert len([item for item in tracked if item.startswith("docs/knowledge/")]) == 6
    attributes = git("check-attr", "eol", "--", "configs/knowledge_manifest.json", "docs/knowledge/credential_abuse_brute_force_response.md")
    assert attributes.count("eol: lf") == 2


def test_generated_candidate_c_state_and_credentials_are_ignored() -> None:
    candidates = [
        "var/knowledge_index/knowledge.sqlite3",
        "var/knowledge_index/chroma/chroma.sqlite3",
        ".env.local",
        "credentials-local.json",
        "developer-service-account.json",
    ]
    output = git("check-ignore", "--", *candidates).splitlines()
    assert set(output) == set(candidates)
    assert subprocess.run(
        ["git", "check-ignore", "--", "configs/knowledge_manifest.json"], cwd=ROOT,
        capture_output=True, text=True,
    ).returncode == 1
