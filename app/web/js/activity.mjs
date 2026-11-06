// Only work started by this page can trigger a save. Reloads never replay it.
export function autoSaveTracker(storage) {
  const started = new Set();
  let saved;
  try { saved = JSON.parse(storage.getItem('muvio-auto-attempts') || '[]'); } catch { saved = []; }
  const attempted = new Set(Array.isArray(saved) ? saved : []);
  return {
    start(id) { if (id) started.add(id); if (started.size > 200) started.delete(started.values().next().value); },
    pending() { return [...started]; },
    clear() { started.clear(); },
    claim(job) {
      if (['error', 'cancelled'].includes(job.status) || (job.status === 'done' && (job.storage_mode !== 'temporary' || job.kind === 'transcribe' || job.availability !== 'available'))) {
        started.delete(job.id);
        return false;
      }
      if (!started.has(job.id) || attempted.has(job.id) || job.status !== 'done' || job.availability !== 'available'
          || job.storage_mode !== 'temporary' || !['download', 'subtitle'].includes(job.kind)) return false;
      attempted.add(job.id);
      if (attempted.size > 200) attempted.delete(attempted.values().next().value);
      started.delete(job.id);
      try { storage.setItem('muvio-auto-attempts', JSON.stringify([...attempted].slice(-200))); } catch { /* private mode */ }
      return true;
    },
  };
}

export function retentionLabel(job) {
  if (job.availability === 'expired') return 'Expired';
  if (job.availability === 'missing' && job.kind !== 'model') return 'Not available';
  if (job.status !== 'done' || job.kind === 'model') return '';
  return job.storage_mode === 'temporary' && job.expires_at
    ? `Temporary · available until ${new Date(job.expires_at).toLocaleString()}` : 'Kept on the server';
}
