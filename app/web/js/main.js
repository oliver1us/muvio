// Dependency-free frontend for the Muvio JSON API.
import { chooseVideo, matchingVideo, codecLabels, containerLabels, groupSubtitles, subtitleLabel } from './media.mjs';

const $ = id => document.getElementById(id);
const number = new Intl.NumberFormat('en', { maximumFractionDigits: 1 });
let current = null;
let currentUrl = '';
let tracks = [];
let jobs = [];
let refreshVersion = 0;
let infoVersion = 0;
let toastTimer;
let pollTimer;
let eventStream;
let eventTimer;
let jobLimit = 30;
let settingsFocus;
const completedLinks = new Map();

function show(text) {
  const node = $('drawer').hidden ? $('message') : $('settingsMessage');
  node.hidden = !text;
  node.textContent = text;
  if (text && node === $('message')) node.focus({ preventScroll: false });
}

function dismissToast() {
  clearTimeout(toastTimer);
  $('activityToast').hidden = true;
  $('toastText').textContent = '';
}

function notify(text, withActivity = true) {
  clearTimeout(toastTimer);
  $('activityToast').hidden = false;
  $('toastText').textContent = text;
  $('toastActivity').hidden = !withActivity;
  toastTimer = setTimeout(dismissToast, 4500);
}

async function api(path, options = {}) {
  const key = localStorage.getItem('muvio-api-key');
  const headers = {
    ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
    ...(key ? { Authorization: `Api-Key ${key}` } : {}),
  };
  let response;
  try { response = await fetch('/api' + path, { ...options, headers: { ...headers, ...options.headers } }); }
  catch { throw new Error('Could not reach the server. Check your connection and try again.'); }
  let result;
  try { result = await response.json(); }
  catch { throw new Error('The server returned an unreadable response. Please try again.'); }
  if (!response.ok || result.status === 'error') {
    throw new Error(result.error?.context?.message || 'The request failed. Please try again.');
  }
  return result;
}

async function busy(button, action) {
  if (button.disabled) return;
  const content = [...button.childNodes].map(node => node.cloneNode(true));
  button.disabled = true;
  button.dataset.busy = 'true';
  button.setAttribute('aria-busy', 'true');
  button.textContent = 'Working…';
  try { await action(); }
  catch (error) { show(error.message); }
  finally {
    delete button.dataset.busy;
    button.removeAttribute('aria-busy');
    button.replaceChildren(...content);
    button.disabled = false;
    if (current) { updateVideo(); updateAudio(); updateSubtitle(); }
  }
}

function option(value, label) { return new Option(label, value); }

function setTab(name, focus = false, sync = true) {
  let active = [...document.querySelectorAll('.tab')].find(tab => tab.dataset.tab === name);
  if (!active || active.disabled) active = [...document.querySelectorAll('.tab')].find(tab => !tab.disabled);
  if (!active) return;
  document.querySelectorAll('.tab').forEach(tab => {
    const selected = tab === active;
    tab.classList.toggle('active', selected);
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    $(tab.getAttribute('aria-controls')).hidden = !selected;
  });
  if (focus) active.focus();
  if (sync) history.replaceState(null, '', `#${active.dataset.tab}`);
}

function videoSettings() {
  return { codec: $('codec').value, container: $('container').value, quality: $('quality').value, embed: Boolean($('embedSubtitle').value) };
}

function updateVideo() {
  if (!current) return;
  const options = current.videoOptions || [];
  const settings = videoSettings();
  const hasAudio = Boolean(current.audioStreams?.length);
  $('videoTagline').textContent = hasAudio ? 'A ready-to-play video, with sound.' : 'A ready-to-play video. This source has no sound.';
  for (const item of $('codec').options) {
    item.disabled = item.value !== 'auto' && !matchingVideo(options, { ...settings, codec: item.value }).length;
  }
  for (const item of $('container').options) {
    item.disabled = item.value !== 'auto' && !matchingVideo(options, { ...settings, container: item.value }).length;
  }
  const available = matchingVideo(options, settings);
  const heights = [...new Set(available.map(item => item.height).filter(Boolean))].sort((a, b) => b - a);
  const quality = settings.quality;
  const qualityOptions = [option('auto', 'Automatic · up to 720p'), option('max', 'Highest available'), ...heights.map(height => option(String(height), `${height}p`))];
  if (!['auto', 'max'].includes(quality) && !heights.includes(Number(quality))) {
    const unavailable = option(quality, `${quality}p · unavailable with these options`);
    unavailable.disabled = true;
    qualityOptions.push(unavailable);
  }
  $('quality').replaceChildren(...qualityOptions);
  $('quality').value = quality;
  const choice = chooseVideo(options, settings);
  const automatic = settings.codec === 'auto' && settings.container === 'auto' && settings.quality === 'auto';
  $('videoBadge').hidden = !automatic || !choice;
  $('videoDownload').disabled = !choice || Boolean($('videoDownload').dataset.busy);
  $('videoWarning').hidden = true;
  if (!choice) {
    $('videoSummary').textContent = options.length ? 'This combination is unavailable' : 'No supported video found';
    $('videoDescription').textContent = options.length ? 'Choose another option, or reset to automatic.' : 'Try the Audio or Subtitles tab instead.';
    $('videoTechnical').textContent = 'MP4 works with H.264 and AAC. WebM requires VP9 or AV1 with Opus or Vorbis. Use MP4 or MKV to include subtitles.';
    $('videoWarning').textContent = settings.embed && settings.container === 'webm'
      ? 'To include subtitles in this app, choose MP4, MKV, or Automatic.'
      : 'The selected format, codec, and resolution must be available together. No download will start with this combination.';
    $('videoWarning').hidden = !options.length;
    return;
  }
  const resolution = choice.height ? `${choice.height}p` : 'source resolution';
  $('videoSummary').textContent = automatic ? `Automatic (${containerLabels[choice.container]} · ${resolution})` : `${containerLabels[choice.container]} · ${resolution}`;
  $('videoDescription').textContent = automatic
    ? (choice.height > 720 ? 'No version at or below 720p is available. Using the lowest source resolution.' : 'A good balance of quality and file size.')
    : 'Your custom download settings.';
  const audio = !hasAudio ? 'No audio track' : choice.container === 'mp4' ? 'AAC audio' : choice.container === 'webm' ? 'Opus / Vorbis audio' : 'Original audio';
  $('videoTechnical').textContent = `${codecLabels[choice.codec]} video · ${audio} · No video re-encoding${settings.embed ? ' · Subtitles included' : ''}`;
  if (choice.codec !== 'h264' || choice.container === 'mkv') {
    $('videoWarning').textContent = `${choice.container === 'mkv' ? `MKV with ${codecLabels[choice.codec]}` : codecLabels[choice.codec]} needs a compatible player. Older devices may not support it.`;
    $('videoWarning').hidden = false;
  }
}

function updateAudio() {
  if (!current) return;
  const format = $('audioFormat').value;
  const automatic = format === 'mp3' && $('bitrate').value === '320';
  const hasAudio = Boolean(current.audioStreams?.length);
  $('audioDownload').disabled = !hasAudio || Boolean($('audioDownload').dataset.busy);
  $('audioSummary').textContent = !hasAudio ? 'No audio found' : automatic ? 'Automatic (MP3)' : format === 'best' ? 'Original audio' : ['wav', 'flac'].includes(format) ? format.toUpperCase() : `${format.toUpperCase()} · ${$('bitrate').value} kbps`;
  $('audioBadge').hidden = !hasAudio || !automatic;
  $('bitrate').disabled = ['best', 'wav', 'flac'].includes(format);
  $('cover').disabled = !['mp3', 'm4a'].includes(format);
  $('audioDescription').textContent = !hasAudio ? 'This source does not contain an audio track.' : format === 'best' ? 'Keep the source audio without another conversion.' : ['wav', 'flac'].includes(format) ? 'Preserves the received audio, not quality already lost at the source.' : 'High-quality audio. Easy to play and share.';
  $('audioHint').textContent = format === 'best' ? 'The source determines the file format. It may need a compatible player.' : ['wav', 'flac'].includes(format) ? 'A larger lossless file cannot recover information missing from the source.' : 'A higher output bitrate does not restore quality missing from the source.';
}

function selectedTrack() { return tracks.find(track => track.key === $('subtitleTrack').value); }

function updateSubtitle() {
  const track = selectedTrack();
  $('subtitleDownload').disabled = !track || Boolean($('subtitleDownload').dataset.busy);
  $('embedSelected').disabled = !track || !current?.videoOptions?.length;
  $('subtitleInfo').textContent = track ? `${$('subtitleFormat').value.toUpperCase()} file · ${track.is_automatic ? 'Generated by the source; accuracy may vary.' : 'Original source subtitles.'}` : 'No matching language. Try a different search.';
}

function renderSubtitles() {
  const previous = $('subtitleTrack').value;
  const search = $('subtitleSearch').value.trim().toLowerCase();
  const visible = tracks.filter(track => `${subtitleLabel(track)} ${track.lang}`.toLowerCase().includes(search));
  $('subtitleTrack').replaceChildren(...visible.map(track => option(track.key, subtitleLabel(track))));
  if (visible.some(track => track.key === previous)) $('subtitleTrack').value = previous;
  updateSubtitle();
}

function renderPicker(items) {
  current = null;
  $('panel').hidden = true;
  $('picker').hidden = false;
  $('pickerItems').replaceChildren();
  for (const item of items) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'picker-item';
    button.textContent = item.title || item.id || 'Untitled item';
    button.onclick = () => busy(button, async () => {
      if (!/^https?:\/\//.test(item.url || '')) throw new Error('This item has no public link. Open it on the source website instead.');
      $('url').value = item.url;
      await fetchInfo(item.url);
    });
    $('pickerItems').append(button);
  }
}

function renderInfo(data, url) {
  current = data;
  currentUrl = url;
  $('picker').hidden = true;
  $('panel').hidden = false;
  $('title').textContent = data.title || 'Untitled media';
  const seconds = Math.floor(data.duration || 0);
  const duration = seconds ? `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}` : '';
  $('byline').textContent = [data.uploader, duration].filter(Boolean).join(' · ');
  $('thumb').hidden = !data.thumbnail;
  if (data.thumbnail) $('thumb').src = data.thumbnail;
  else $('thumb').removeAttribute('src');
  for (const id of ['codec', 'container', 'quality']) $(id).value = 'auto';
  $('videoAdvanced').open = false;
  $('audioFormat').value = 'mp3';
  $('bitrate').value = '320';
  $('cover').checked = true;
  $('ai').checked = false;
  $('aiLabel').textContent = `Send transcript to optional AI cleanup · about ${number.format(Math.ceil((data.duration || 60) * 3))} tokens`;
  tracks = groupSubtitles(data.subtitles || [], navigator.languages);
  $('subtitleSearch').value = '';
  $('subtitleFormat').value = 'srt';
  $('subtitleControls').hidden = !tracks.length;
  $('noSubtitles').hidden = Boolean(tracks.length);
  $('subtitleSearchLabel').hidden = tracks.length <= 8;
  $('embedSubtitle').replaceChildren(option('', 'None'), ...tracks.map(track => option(track.key, subtitleLabel(track))));
  $('embedSubtitle').value = '';
  $('videoTab').disabled = !data.videoOptions?.length;
  $('videoTab').title = data.videoOptions?.length ? '' : 'No supported video is available for this link';
  $('audioTab').disabled = !data.audioStreams?.length;
  updateVideo();
  updateAudio();
  renderSubtitles();
  setTab(location.hash.slice(1) || (data.videoOptions?.length ? 'video' : 'audio'));
}

async function fetchInfo(url) {
  const version = ++infoVersion;
  show('');
  const data = await api('/info?url=' + encodeURIComponent(url));
  if (version !== infoVersion) return;
  if (data.entries?.length) renderPicker(data.entries);
  else renderInfo(data, url);
}

function renderJobs() {
  if ($('activityPanel').hidden) return;
  const focusId = document.activeElement?.dataset.actionId;
  const list = $('jobs');
  list.replaceChildren();
  if (!jobs.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-state';
    empty.textContent = 'Your downloads will appear here.';
    list.append(empty);
  }
  for (const job of jobs.slice(0, jobLimit)) {
    const row = document.createElement('article');
    row.className = 'job';
    const details = document.createElement('div');
    details.className = 'job-copy';
    const name = document.createElement('strong');
    name.textContent = job.filename || (job.kind === 'transcribe' ? 'Transcription' : job.kind === 'subtitle' ? 'Subtitles' : job.kind === 'model' ? 'Model download' : 'Media download');
    if (!job.filename && job.url) name.title = job.url;
    const status = document.createElement('p');
    status.className = `job-status ${job.status}`;
    const percent = `${number.format(Math.round(job.percent || 0))}%`;
    status.textContent = ({ queued: 'In queue', running: `${job.stage || 'Preparing'} · ${percent}`, done: job.kind === 'model' ? 'Model installed' : 'Ready to save', error: 'Could not finish', cancelled: 'Cancelled' })[job.status] || job.status;
    details.append(name, status);
    if (job.status === 'running') {
      const bar = document.createElement('progress');
      bar.value = job.percent || 0;
      bar.max = 100;
      bar.setAttribute('aria-label', `${name.textContent} progress`);
      details.append(bar);
    }
    if (job.error || job.warning) {
      const notice = document.createElement('p');
      notice.className = 'job-notice';
      notice.textContent = job.error?.context?.message || job.warning;
      details.append(notice);
    }
    if (job.speed || job.eta) {
      const technical = document.createElement('details');
      technical.className = 'job-details';
      const summary = document.createElement('summary');
      summary.textContent = 'Transfer details';
      const text = document.createElement('p');
      text.className = 'muted';
      text.textContent = [job.speed ? `${number.format(job.speed / 1048576)} MB/s` : '', job.eta ? `About ${number.format(Math.round(job.eta))} seconds left` : ''].filter(Boolean).join(' · ');
      technical.append(summary, text);
      details.append(technical);
    }
    const actions = document.createElement('div');
    actions.className = 'job-actions';
    if (job.status === 'done' && job.kind !== 'model') {
      let cached = completedLinks.get(job.id);
      if (!cached || cached.expires < Date.now()) {
        cached = { expires: Date.now() + 5 * 60 * 1000 };
        completedLinks.set(job.id, cached);
        api('/jobs/' + job.id).then(detail => { cached.detail = detail; renderJobs(); })
          .catch(error => { cached.error = error.message; renderJobs(); });
      }
      if (cached.detail) {
        const downloads = cached.detail.downloads || {};
        for (const [ext, url] of Object.entries(Object.keys(downloads).length ? downloads : { download: cached.detail.url })) {
          if (!url) continue;
          const link = document.createElement('a');
          link.href = url;
          link.dataset.actionId = `${job.id}:${ext}`;
          link.textContent = ext === 'download' ? 'Save file ↓' : `${ext.toUpperCase()} ↓`;
          actions.append(link);
        }
      } else {
        const label = document.createElement('span');
        label.className = 'muted';
        label.textContent = cached.error ? 'Link unavailable. Try Refresh.' : 'Getting file link…';
        actions.append(label);
      }
    } else if (['queued', 'running'].includes(job.status)) {
      const cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.dataset.actionId = `${job.id}:cancel`;
      cancel.className = 'quiet';
      cancel.textContent = 'Cancel';
      cancel.onclick = () => busy(cancel, async () => {
        if (!window.confirm('Cancel this job?')) return;
        await api('/jobs/' + job.id, { method: 'DELETE' });
        await refresh();
      });
      actions.append(cancel);
    }
    row.append(details, actions);
    list.append(row);
  }
  if (jobs.length > jobLimit) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'text-button';
    more.textContent = 'Show older activity';
    more.onclick = () => { jobLimit += 30; renderJobs(); };
    list.append(more);
  }
  if (focusId) [...list.querySelectorAll('[data-action-id]')].find(node => node.dataset.actionId === focusId)?.focus({ preventScroll: true });
}

async function refresh() {
  const version = ++refreshVersion;
  let updated;
  try { updated = await api('/jobs'); }
  catch (error) { if (version === refreshVersion && !$('activityPanel').hidden) show(error.message); return; }
  if (version !== refreshVersion) return;
  const previous = new Map(jobs.map(job => [job.id, job.status]));
  jobs = updated;
  if (jobs.some(job => job.status === 'done' && ['running', 'queued'].includes(previous.get(job.id)))) {
    notify(jobs.some(job => job.status === 'done' && job.kind !== 'model' && ['running', 'queued'].includes(previous.get(job.id))) ? 'Your file is ready to save.' : 'Your model is installed.');
  }
  const active = jobs.filter(job => ['queued', 'running'].includes(job.status));
  $('activityCount').hidden = !active.length;
  $('activityCount').textContent = number.format(active.length);
  const running = active.find(job => job.status === 'running');
  $('activityProgress').hidden = !active.length;
  $('activityProgress').textContent = running ? `${number.format(Math.round(running.percent || 0))}%` : 'Queued';
  $('activityButton').setAttribute('aria-label', active.length ? `Activity, ${active.length} active, ${$('activityProgress').textContent}` : 'Activity');
  renderJobs();
}

function activity(state) {
  $('activityPanel').hidden = !state;
  $('activityButton').setAttribute('aria-expanded', String(state));
  if (state) { dismissToast(); refresh(); }
}

function drawer(state) {
  if (state) { settingsFocus = document.activeElement; activity(false); dismissToast(); }
  $('drawer').hidden = !state;
  $('overlay').hidden = !state;
  $('main').inert = state;
  document.querySelector('.header').inert = state;
  document.body.classList.toggle('dialog-open', state);
  if (state) { $('settingsMessage').hidden = true; $('closeSettings').focus(); }
  else settingsFocus?.focus();
}

function startUpdates() {
  clearInterval(pollTimer);
  clearTimeout(eventTimer);
  eventTimer = null;
  eventStream?.close();
  const poll = () => { eventStream?.close(); clearInterval(pollTimer); pollTimer = setInterval(refresh, 2500); };
  if (localStorage.getItem('muvio-api-key')) { poll(); return; }
  try {
    eventStream = new EventSource('/api/events');
    eventStream.addEventListener('progress', () => {
      if (!eventTimer) eventTimer = setTimeout(() => { eventTimer = null; refresh(); }, 250);
    });
    eventStream.onerror = poll;
  } catch { poll(); }
}

$('fetchForm').onsubmit = event => { event.preventDefault(); busy($('fetch'), () => fetchInfo($('url').value.trim())); };
$('paste').onclick = () => busy($('paste'), async () => {
  if (!navigator.clipboard?.readText) throw new Error('Clipboard access is unavailable. Paste the link into the field instead.');
  try { $('url').value = await navigator.clipboard.readText(); $('url').focus(); }
  catch { throw new Error('Clipboard permission was denied. Paste the link into the field instead.'); }
});

document.querySelectorAll('.tab').forEach(button => {
  button.onclick = () => setTab(button.dataset.tab);
  button.onkeydown = event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...document.querySelectorAll('.tab')].filter(tab => !tab.disabled);
    const index = tabs.indexOf(button);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    setTab(tabs[next].dataset.tab, true);
  };
});
window.addEventListener('hashchange', () => { if (current) setTab(location.hash.slice(1), false, false); });
for (const id of ['quality', 'codec', 'container', 'embedSubtitle']) $(id).onchange = updateVideo;
$('resetVideo').onclick = () => { for (const id of ['quality', 'codec', 'container']) $(id).value = 'auto'; updateVideo(); };
for (const id of ['audioFormat', 'bitrate']) $(id).onchange = updateAudio;
$('subtitleSearch').oninput = renderSubtitles;
$('subtitleTrack').onchange = $('subtitleFormat').onchange = updateSubtitle;
$('embedSelected').onclick = () => { const track = selectedTrack(); if (!track) return; $('embedSubtitle').value = track.key; updateVideo(); $('videoAdvanced').open = true; setTab('video', true); };

for (const mode of ['video', 'audio']) $(mode + 'Download').onclick = () => busy($(mode + 'Download'), async () => {
  if (!current) return;
  const settings = videoSettings();
  const track = tracks.find(item => item.key === $('embedSubtitle').value);
  const payload = {
    url: currentUrl, downloadMode: mode,
    ...(mode === 'video' ? { videoQuality: settings.quality, videoCodec: settings.codec, videoContainer: settings.container,
      subtitleLang: track?.lang || null, subtitleAutomatic: Boolean(track?.is_automatic), embedSubtitles: Boolean(track) } : {
      audioFormat: $('audioFormat').value, audioBitrate: $('bitrate').value, embedThumbnail: !$('cover').disabled && $('cover').checked,
    }),
  };
  const result = await api('/download', { method: 'POST', body: JSON.stringify(payload) });
  if (result.status === 'picker') { renderPicker(result.picker); return; }
  notify(`${mode === 'video' ? 'Video' : 'Audio'} download added to Activity.`);
  await refresh();
});
$('subtitleDownload').onclick = () => busy($('subtitleDownload'), async () => {
  const track = selectedTrack();
  if (!track) return;
  await api('/subtitle-download', { method: 'POST', body: JSON.stringify({ url: currentUrl, lang: track.lang, automatic: track.is_automatic, format: $('subtitleFormat').value }) });
  notify('Subtitle download added to Activity.');
  await refresh();
});
const transcriptionOptions = () => ({ mode: $('mode').value, language: $('language').value || null, vocalFocus: $('focus').checked, separateVocals: $('separate').checked, aiCleanup: $('ai').checked });
$('transcribe').onclick = () => busy($('transcribe'), async () => {
  await api('/transcribe', { method: 'POST', body: JSON.stringify({ ...transcriptionOptions(), url: currentUrl, title: current?.title || '', artist: current?.uploader || '' }) });
  notify('Transcription added to Activity.');
  await refresh();
});
$('uploadButton').onclick = () => busy($('uploadButton'), async () => {
  if (!$('upload').files[0]) throw new Error('Choose an audio or video file first.');
  const form = new FormData();
  form.append('file', $('upload').files[0]);
  form.append('options', JSON.stringify(transcriptionOptions()));
  await api('/transcribe', { method: 'POST', body: form });
  notify('File transcription added to Activity.');
  await refresh();
});
$('refresh').onclick = () => { for (const [id, cached] of completedLinks) if (cached.error) completedLinks.delete(id); refresh(); };
$('activityButton').onclick = () => activity($('activityPanel').hidden);
$('toastActivity').onclick = () => { activity(true); $('activityButton').focus(); };
$('dismissToast').onclick = dismissToast;
for (const name of ['mouseenter', 'focusin']) $('activityToast').addEventListener(name, () => clearTimeout(toastTimer));
for (const name of ['mouseleave', 'focusout']) $('activityToast').addEventListener(name, () => { clearTimeout(toastTimer); toastTimer = setTimeout(dismissToast, 4500); });
document.addEventListener('click', event => { if (!$('activityPanel').hidden && !event.target.closest('.activity-control')) activity(false); });
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    if (!$('drawer').hidden) drawer(false);
    else if (!$('activityPanel').hidden) { activity(false); $('activityButton').focus(); }
    else if (!$('activityToast').hidden) { dismissToast(); $('activityButton').focus(); }
  }
  if (event.key === 'Tab' && !$('drawer').hidden) {
    const controls = [...$('drawer').querySelectorAll('button, input, select, [tabindex="0"]')].filter(node => !node.disabled && !node.hidden);
    const first = controls[0], last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});

$('settingsButton').onclick = async () => {
  drawer(true);
  $('authKey').value = localStorage.getItem('muvio-api-key') || '';
  try {
    const data = await api('/settings');
    $('demucs').checked = data.demucs_enabled;
    $('whisperModel').value = data.whisper_model;
    $('llmEnabled').checked = data.enable_llm_cleanup;
    $('llmProvider').value = data.llm_provider;
    $('llmBase').value = data.llm_base_url;
    $('llmModel').value = data.llm_model;
  } catch (error) { show(error.message); }
};
$('closeSettings').onclick = $('overlay').onclick = () => drawer(false);
$('saveSettings').onclick = () => busy($('saveSettings'), async () => {
  localStorage.setItem('muvio-api-key', $('authKey').value);
  const values = { demucs_enabled: $('demucs').checked, whisper_model: $('whisperModel').value,
    enable_llm_cleanup: $('llmEnabled').checked, llm_provider: $('llmProvider').value,
    llm_base_url: $('llmBase').value, llm_model: $('llmModel').value };
  if ($('llmKey').value) values.llm_api_key = $('llmKey').value;
  await api('/settings', { method: 'PUT', body: JSON.stringify(values) });
  $('llmKey').value = '';
  drawer(false);
  notify('Settings saved.', false);
  startUpdates();
  await refresh();
});
for (const [id, path, name] of [['modelDownload', 'whisper', 'Whisper'], ['demucsDownload', 'demucs', 'Demucs']]) $(id).onclick = () => busy($(id), async () => {
  await api('/models/' + path, { method: 'POST' });
  drawer(false);
  notify(`${name} model download added to Activity.`);
  await refresh();
});
refresh();
startUpdates();
