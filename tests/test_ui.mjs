import assert from 'node:assert/strict';
import test from 'node:test';
import { chooseVideo, matchingVideo, groupSubtitles, subtitleLabel } from '../app/web/js/media.mjs';

const choices = heights => heights.map(height => ({ height, codec: 'h264', container: 'mp4' }));

test('automatic resolution targets 720p without inventing source resolutions', () => {
  for (const [heights, expected] of [
    [[2160, 1440, 1080, 720, 480], 720], [[1080, 480, 360], 480],
    [[480, 360, 240], 480], [[240, 144], 240], [[1440, 1080], 1080],
  ]) assert.equal(chooseVideo(choices(heights)).height, expected);
});

test('available codec/container combinations determine the displayed choice', () => {
  const options = [...choices([480]), { height: 1080, codec: 'av1', container: 'webm' }];
  assert.equal(chooseVideo(options).container, 'mp4');
  assert.equal(chooseVideo(options, { quality: 'max' }).height, 1080);
  assert.equal(chooseVideo(options, { quality: '1080' }).codec, 'av1');
  assert.equal(chooseVideo(options, { quality: '1080', codec: 'h264' }), null);
  assert.equal(chooseVideo(options, { container: 'webm', codec: 'h264' }), null);
  assert.equal(chooseVideo(options, { quality: '240' }), null);
});

test('the automatic resolution cap applies across codecs before compatibility preference', () => {
  const options = [...choices([1080, 480]), { height: 720, codec: 'vp9', container: 'webm' }];
  assert.deepEqual(chooseVideo(options), { height: 720, codec: 'vp9', container: 'webm' });
  assert.equal(chooseVideo([...choices([1440]), { height: 1080, codec: 'vp9', container: 'webm' }]).height, 1080);
});

test('fallback is shown with its real format; subtitle embedding filters WebM', () => {
  const options = [{ codec: 'vp9', container: 'webm', height: 720 }, { codec: 'vp9', container: 'mkv', height: 720 }];
  assert.equal(chooseVideo(options).container, 'webm');
  assert.equal(chooseVideo(options, { embed: true }).container, 'mkv');
  assert.deepEqual(matchingVideo(options, { container: 'webm', embed: true }), []);
});

test('subtitle formats are deduplicated without losing original and automatic tracks', () => {
  const tracks = groupSubtitles([
    { lang: 'fr', ext: 'vtt', is_automatic: false },
    { lang: 'en', ext: 'vtt', is_automatic: true },
    { lang: 'en', ext: 'srt', is_automatic: false },
    { lang: 'en', ext: 'vtt', is_automatic: false },
  ], ['en-US']);
  assert.equal(tracks.length, 3);
  assert.equal(tracks[0].key, 'en:false');
  assert.equal(tracks[1].key, 'en:true');
  assert.equal(subtitleLabel(tracks[0]), 'English · Original');
});

test('missing combinations and empty subtitle lists have safe empty states', () => {
  assert.equal(chooseVideo([]), null);
  assert.deepEqual(groupSubtitles([]), []);
});
