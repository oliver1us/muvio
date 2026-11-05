// Pure presentation helpers. Keep the preference order in sync with engine/formats.py.
const preferences = [
  ['h264', 'mp4'], ['vp9', 'webm'], ['av1', 'webm'], ['h264', 'mkv'],
  ['vp9', 'mp4'], ['av1', 'mp4'], ['vp9', 'mkv'], ['av1', 'mkv'],
];

export const codecLabels = { h264: 'H.264', vp9: 'VP9', av1: 'AV1' };
export const containerLabels = { mp4: 'MP4', webm: 'WebM', mkv: 'MKV' };

export function matchingVideo(options, { codec = 'auto', container = 'auto', embed = false } = {}) {
  return options.filter(option =>
    (codec === 'auto' || option.codec === codec) &&
    (container === 'auto' || option.container === container) &&
    (!embed || ['mp4', 'mkv'].includes(option.container)));
}

export function chooseVideo(options, settings = {}) {
  let candidates = matchingVideo(options, settings);
  const quality = settings.quality || 'auto';
  if (!['auto', 'max'].includes(quality)) {
    candidates = candidates.filter(option => option.height === Number(quality));
  }
  if (quality === 'auto' && candidates.length) {
    const below = candidates.filter(option => option.height > 0 && option.height <= 720);
    const target = below.length ? Math.max(...below.map(option => option.height)) : Math.min(...candidates.map(option => option.height));
    candidates = candidates.filter(option => option.height === target);
  } else if (quality === 'max' && candidates.length) {
    const highest = Math.max(...candidates.map(option => option.height));
    candidates = candidates.filter(option => option.height === highest);
  }
  for (const [codec, container] of preferences) {
    const preferred = candidates.filter(option => option.codec === codec && option.container === container);
    if (!preferred.length) continue;
    return preferred.reduce((a, b) => a.height >= b.height ? a : b);
  }
  return null;
}

export function groupSubtitles(tracks, languages = ['en']) {
  const unique = new Map();
  for (const track of tracks) {
    const key = `${track.lang}:${Boolean(track.is_automatic)}`;
    if (!unique.has(key)) unique.set(key, { ...track, key });
  }
  const preferred = languages.map(language => language.toLowerCase().split('-')[0]);
  const rank = track => {
    const index = preferred.indexOf(track.lang.toLowerCase().split('-')[0]);
    return index === -1 ? preferred.length : index;
  };
  return [...unique.values()].sort((a, b) => rank(a) - rank(b) ||
    a.lang.localeCompare(b.lang, 'en') || Number(a.is_automatic) - Number(b.is_automatic));
}

export function subtitleLabel(track) {
  let language;
  try { language = new Intl.DisplayNames(['en'], { type: 'language' }).of(track.lang); } catch { /* Nonstandard source language code. */ }
  return `${language || track.name || track.lang} · ${track.is_automatic ? 'Auto-generated' : 'Original'}`;
}
