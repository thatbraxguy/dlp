# Test-only site extractor for the borrow tests. "faketube.example" does not exist; the
# tests serve clones of its pages on 127.0.0.1, which this extractor's pattern does not match.
from yt_dlp.extractor.common import InfoExtractor


class FakeTubeIE(InfoExtractor):
    _VALID_URL = r'https?://(?:www\.)?faketube\.example/ft-watch/(?P<id>\d+)'
    _TESTS = [{'url': 'https://www.faketube.example/ft-watch/10001', 'only_matching': True}]

    def _real_extract(self, url):
        video_id = self._match_id(url)
        webpage = self._download_webpage(url, video_id)
        key = self._search_regex(r'data-ft-key="([^"]+)"', webpage, 'player key')
        # the media URL is only in the API answer, which lives on the extractor's own site
        meta = self._download_json(
            f'https://www.faketube.example/api/video/{video_id}.json', video_id, query={'k': key})
        return {
            'id': video_id,
            'title': meta['title'],
            'formats': [{
                'url': f'https://www.faketube.example/media/{meta["slug"]}/clip_180p.mp4',
                'height': 180,
                'format_id': 'ft-180p',
            }],
        }
