from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prehrajto_search import parse_search_html  # noqa: E402
from prepare_episode_sources import source_score  # noqa: E402
from source_quality import source_quality_score, source_quality_tier  # noqa: E402


class PrehrajtoSearchTest(unittest.TestCase):
    def test_parses_result_metadata(self) -> None:
        page = """
        <div class="video-wrapper">
          <div class="video__picture--container" data-video-id="26210967">
            <a class="video video--small video--link"
               href="/show-s07e19-cz-1080p/5e32690144acb11b"
               title="Show S07E19 CZ 1080p">
              <div class="video__tag video__tag--time">00:17:59</div>
              <span class="format__text">HD</span>
              <div class="video__tag video__tag--size video__tag--size-alone">317.79 MB</div>
            </a>
          </div>
        </div>
        """
        rows = parse_search_html(page)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].external_id, "5e32690144acb11b")
        self.assertEqual(rows[0].duration_sec, 1079)
        self.assertEqual(rows[0].filesize_bytes, int(317.79 * 1024**2))

    def test_1080_title_is_preferred(self) -> None:
        source = {
            "title": "Show S07E19 CZ 1080p",
            "resolution_hint": "HD",
            "filesize_bytes": int(286.34 * 1024**2),
        }
        self.assertEqual(source_quality_tier(source), "preferred")
        self.assertEqual(source_quality_score(source)[1], 1080)

    def test_preferred_live_source_beats_small_database_source(self) -> None:
        live_source = {
            "provider": "prehrajto",
            "source_id": -1,
            "source_title": "Show S07E19 CZ 1080p",
            "filesize_bytes": 300_249_251,
        }
        database_source = {
            "provider": "prehrajto",
            "source_id": 1,
            "source_title": "Show S07E19 CZ",
            "resolution_hint": "720p",
            "filesize_bytes": 117_792_114,
        }
        live_result = {
            "verdict": "PROBABLE_CZ_AUDIO",
            "detected_by": "title",
            "signals": {"provider_probe": {}},
        }
        database_result = {
            "verdict": "CZ_AUDIO",
            "detected_by": "metadata",
            "signals": {"provider_probe": {}},
        }
        self.assertGreater(
            source_score(live_result, live_source),
            source_score(database_result, database_source),
        )


if __name__ == "__main__":
    unittest.main()
