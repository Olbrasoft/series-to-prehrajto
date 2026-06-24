import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import prepare_episode_sources as source_preparation  # noqa: E402


def test_completed_prepared_episodes_include_no_source_results(tmp_path):
    path = tmp_path / "prepared.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"episode_id": 1, "selected_source": None}),
                json.dumps({"episode_id": 2, "selected_source": {"source_id": 20}}),
                json.dumps({"episode_id": 3, "selected_source": {"source_id": 30}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert source_preparation.latest_completed_prepared_episode_ids(path, {30}) == {1, 2}


def test_uploaded_episode_exclusions_include_ids_and_keys(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "uploaded-shard-0.json").write_text(
        json.dumps(
            {
                "uploads": [
                    {
                        "episode_id": 10,
                        "series_id": 2,
                        "season": 3,
                        "episode": 4,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(source_preparation, "REPO_ROOT", tmp_path)

    uploaded_ids, uploaded_keys = source_preparation.uploaded_episode_exclusions()

    assert uploaded_ids == {10}
    assert uploaded_keys == {(2, 3, 4)}
