(() => {
  const $ = (id) => document.getElementById(id);

  const headerMark = $('header-mark');
  const urlInput = $('url-input');
  const btnFetch = $('btn-fetch');
  const btnClear = $('btn-clear');
  const infoCard = $('info-card');
  const infoThumb = $('info-thumb');
  const infoTitle = $('info-title');
  const infoType = $('info-type');
  const infoDuration = $('info-duration');
  const infoChannel = $('info-channel');
  const infoViews = $('info-views');
  const infoSize = $('info-size');
  const qualitySelect = $('quality-select');
  const qualityPills = $('quality-pills');
  const cookieInput = $('cookie-input');
  const btnUploadCookies = $('btn-upload-cookies');
  const chipFfmpeg = $('chip-ffmpeg');
  const chipCookies = $('chip-cookies');

  const folderInput = $('folder-input');
  const btnBrowseFolder = $('btn-browse-folder');
  const btnSetFolder = $('btn-set-folder');

  const statusLine = $('status-line');
  const meterFill = $('meter-fill');
  const readoutPct = $('readout-pct');
  const readoutSpeed = $('readout-speed');
  const readoutEta = $('readout-eta');
  const btnDownload = $('btn-download');
  const btnPause = $('btn-pause');
  const btnCancel = $('btn-cancel');

  const btnRefresh = $('btn-refresh');
  const downloadsBody = $('downloads-body');
  const toast = $('toast');
  const themeToggle = $('theme-toggle');
  const metaThemeColor = document.querySelector('meta[name="theme-color"]');

  let currentJobId = null;
  let pollTimer = null;
  let isPaused = false;

  // ---------- helpers ----------

  function showToast(message, kind = 'default') {
    toast.textContent = message;
    toast.className = 'toast' + (kind === 'error' ? ' is-error' : kind === 'success' ? ' is-success' : '');
    toast.hidden = false;
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => { toast.hidden = true; }, 3500);
  }

  async function api(path, options = {}) {
    const res = await fetch(path, {
      headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
      ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || data.message || 'Request failed');
    return data;
  }

  function normalizeUrl(u) {
    u = (u || '').trim();
    if (u && !/^https?:\/\//i.test(u)) u = 'https://' + u;
    return u;
  }

  function setActiveIndicators(active) {
    if (headerMark) headerMark.classList.toggle('is-active', active);
    if (meterFill) meterFill.classList.toggle('is-active', active);
  }

  // ---------- status chips + folder ----------

  async function refreshStatus() {
    try {
      const s = await api('/api/status');
      chipFfmpeg.classList.toggle('is-ok', s.ffmpeg_available);
      chipFfmpeg.classList.toggle('is-warn', !s.ffmpeg_available);
      chipCookies.classList.toggle('is-ok', s.cookie_available);
      chipCookies.classList.toggle('is-warn', !s.cookie_available);

      // Only overwrite the folder field if the user hasn't typed something
      // of their own yet, so we don't clobber an in-progress edit.
      if (folderInput && document.activeElement !== folderInput && !folderInput.value) {
        folderInput.value = s.download_folder || '';
      }
    } catch (e) { /* ignore */ }
  }

  // ---------- download folder ----------

  async function browseFolder() {
    btnBrowseFolder.disabled = true;
    btnBrowseFolder.textContent = 'Opening…';
    try {
      const res = await api('/api/browse-folder', { method: 'POST' });
      if (res.folder) {
        folderInput.value = res.folder;
        showToast('Download folder set', 'success');
      }
      // res.folder === null means the user closed the dialog without picking one.
    } catch (e) {
      showToast(e.message, 'error');
    } finally {
      btnBrowseFolder.disabled = false;
      btnBrowseFolder.textContent = '📁 Browse…';
    }
  }

  async function setFolder() {
    const folder = (folderInput.value || '').trim();
    if (!folder) { showToast('Enter a folder path first', 'error'); return; }
    try {
      const res = await api('/api/set-folder', { method: 'POST', body: JSON.stringify({ folder }) });
      folderInput.value = res.folder;
      showToast('Download folder set', 'success');
      loadDownloads();
    } catch (e) {
      showToast(e.message, 'error');
    }
  }

  // ---------- fetch info ----------

  async function fetchInfo() {
    const url = normalizeUrl(urlInput.value);
    if (!url) { showToast('Enter a YouTube URL first', 'error'); return; }

    btnFetch.disabled = true;
    btnFetch.textContent = 'Fetching…';
    try {
      const info = await api('/api/fetch-info', {
        method: 'POST',
        body: JSON.stringify({ url, quality: qualitySelect.value }),
      });
      infoCard.hidden = false;

      if (info.is_playlist) {
        infoTitle.textContent = `Playlist: ${info.title}`;
        infoType.textContent = `Playlist (${info.video_count} videos)`;
        infoDuration.textContent = 'Multiple videos';
        infoChannel.textContent = info.uploader;
        infoViews.textContent = `${info.video_count} videos`;
        infoSize.textContent = '—';
        if (info.thumbnail) {
          infoThumb.src = info.thumbnail;
          infoThumb.hidden = false;
        } else {
          infoThumb.hidden = true;
          infoThumb.removeAttribute('src');
        }
      } else {
        infoTitle.textContent = info.title;
        infoType.textContent = 'Single video';
        infoDuration.textContent = info.duration;
        infoChannel.textContent = info.uploader;
        infoViews.textContent = info.view_count ? info.view_count.toLocaleString() : '—';
        infoSize.textContent = info.filesize || '—';
        if (info.thumbnail) {
          infoThumb.src = info.thumbnail;
          infoThumb.hidden = false;
        } else {
          infoThumb.hidden = true;
          infoThumb.removeAttribute('src');
        }
      }
      statusLine.textContent = 'Ready to download';
      infoCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (e) {
      showToast(e.message, 'error');
      statusLine.textContent = 'Failed to fetch info';
    } finally {
      btnFetch.disabled = false;
      btnFetch.textContent = 'Fetch info';
    }
  }

  function clearUrl() {
    urlInput.value = '';
    infoCard.hidden = true;
    infoThumb.hidden = true;
    infoThumb.removeAttribute('src');
  }

  // ---------- download lifecycle ----------

  async function startDownload() {
    const url = normalizeUrl(urlInput.value);
    if (!url) { showToast('Enter a YouTube URL first', 'error'); return; }

    resetProgress();
    btnDownload.disabled = true;
    btnPause.disabled = false;
    btnCancel.disabled = false;
    isPaused = false;
    btnPause.textContent = '⏸ Pause';
    statusLine.textContent = 'Starting download…';
    setActiveIndicators(true);

    try {
      const { job_id } = await api('/api/download', {
        method: 'POST',
        body: JSON.stringify({ url, quality: qualitySelect.value }),
      });
      currentJobId = job_id;
      pollTimer = setInterval(pollProgress, 800);
    } catch (e) {
      showToast(e.message, 'error');
      statusLine.textContent = 'Download failed';
      resetControls();
    }
  }

  async function pollProgress() {
    if (!currentJobId) return;
    try {
      const job = await api(`/api/progress/${currentJobId}`);
      updateProgress(job);

      if (job.done) {
        clearInterval(pollTimer);
        pollTimer = null;
        resetControls();

        if (job.status === 'complete') {
          statusLine.textContent = 'Download complete!';
          showToast('Download complete', 'success');
          loadDownloads();
          const finishedFiles = (job.result && job.result.files) || [];
          finishedFiles.forEach((f) => triggerDownload(f.relpath));
        } else if (job.status === 'cancelled') {
          statusLine.textContent = 'Download cancelled';
          resetProgress();
        } else if (job.status === 'error') {
          statusLine.textContent = 'Download failed';
          showToast(job.error || 'Download failed', 'error');
        }
        currentJobId = null;
      }
    } catch (e) {
      clearInterval(pollTimer);
      pollTimer = null;
      resetControls();
    }
  }

  function updateProgress(job) {
    const pct = Math.max(0, Math.min(100, job.percentage || 0));
    meterFill.style.width = pct + '%';
    readoutPct.textContent = pct.toFixed(1) + '%';
    readoutSpeed.textContent = job.speed || '—';
    readoutEta.textContent = job.eta || '--:--';
    statusLine.textContent = job.message || 'Downloading…';
  }

  function resetProgress() {
    meterFill.style.width = '0%';
    readoutPct.textContent = '0%';
    readoutSpeed.textContent = '—';
    readoutEta.textContent = '--:--';
  }

  function resetControls() {
    btnDownload.disabled = false;
    btnPause.disabled = true;
    btnCancel.disabled = true;
    setActiveIndicators(false);
  }

  async function togglePause() {
    if (!currentJobId) return;
    try {
      const res = await api(`/api/pause/${currentJobId}`, { method: 'POST' });
      isPaused = res.paused;
      btnPause.textContent = isPaused ? '⏵ Resume' : '⏸ Pause';
      statusLine.textContent = isPaused ? 'Paused' : 'Downloading…';
      setActiveIndicators(!isPaused);
    } catch (e) { showToast(e.message, 'error'); }
  }

  async function cancelDownload() {
    if (!currentJobId) return;
    try {
      await api(`/api/cancel/${currentJobId}`, { method: 'POST' });
      statusLine.textContent = 'Cancelling…';
    } catch (e) { showToast(e.message, 'error'); }
  }

  // ---------- downloads list ----------

  async function loadDownloads() {
    try {
      const res = await api('/api/downloads');
      const files = res.files || [];
      if (!files.length) {
        downloadsBody.innerHTML = '<tr><td class="table__empty" colspan="5">No files yet</td></tr>';
        return;
      }
      downloadsBody.innerHTML = files.map((f) => `
        <tr>
          <td class="table__name" data-label="File">${escapeHtml(f.name)}</td>
          <td data-label="Type"><span class="tag ${f.type === 'Video' ? 'tag--video' : f.type === 'Audio' ? 'tag--audio' : ''}">${f.type}</span></td>
          <td data-label="Size">${f.size}</td>
          <td data-label="Date">${f.date}</td>
          <td class="table__actions" data-label="">
            <a class="table__link" href="/api/downloads/${encodePath(f.relpath)}" download>Download</a>
            <a class="table__link" href="/api/downloads/play/${encodePath(f.relpath)}" target="_blank" rel="noopener">Play</a>
            <button class="table__link table__link--btn" data-reveal="${encodePath(f.relpath)}" type="button">Open in folder</button>
          </td>
        </tr>
      `).join('');
    } catch (e) { /* ignore */ }
  }

  function encodePath(relpath) {
    return relpath.split('/').map(encodeURIComponent).join('/');
  }

  function triggerDownload(relpath) {
    // A hidden <a download> click forces the browser to save the file
    // straight to disk (its normal downloads folder / download bar),
    // instead of navigating to or streaming the file inline.
    const a = document.createElement('a');
    a.href = `/api/downloads/${encodePath(relpath)}`;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  async function revealFile(relpath) {
    try {
      await api(`/api/downloads/reveal/${relpath}`, { method: 'POST' });
    } catch (e) {
      showToast(e.message || 'Could not open file manager', 'error');
    }
  }

  // ---------- cookies ----------

  async function uploadCookies() {
    const file = cookieInput.files[0];
    if (!file) { showToast('Choose a cookies.txt file first', 'error'); return; }
    const form = new FormData();
    form.append('file', file);
    try {
      await fetch('/api/cookies', { method: 'POST', body: form });
      showToast('Cookies imported', 'success');
      refreshStatus();
    } catch (e) { showToast('Failed to import cookies', 'error'); }
  }

  // ---------- theme ----------

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    if (metaThemeColor) metaThemeColor.setAttribute('content', theme === 'light' ? '#f4f5f7' : '#0a0b0d');
    if (themeToggle) themeToggle.setAttribute('aria-label', theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode');
    try { localStorage.setItem('ytdl-theme', theme); } catch (e) { /* ignore */ }
  }

  if (themeToggle) {
    themeToggle.addEventListener('click', () => {
      const current = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
      applyTheme(current === 'light' ? 'dark' : 'light');
    });
    // Sync the button's label/state with whatever the anti-flash inline
    // script in <head> already applied before this file loaded.
    applyTheme(document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark');
  }

  // ---------- mobile quality pills ----------
  // The <select> stays the source of truth (startDownload reads its
  // .value); pills just drive it visually on small screens.
  let refetchTimer = null;
  // Add this function to app.js
async function testCookies() {
    const btn = document.getElementById('btn-test-cookies');
    btn.disabled = true;
    btn.textContent = 'Testing…';
    
    try {
        const result = await api('/api/test-cookies', { method: 'POST' });
        if (result.ok) {
            showToast(result.message, 'success');
            refreshStatus();
        } else {
            showToast(result.message || 'Cookies not working', 'error');
        }
    } catch (e) {
        showToast(e.message || 'Failed to test cookies', 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = `
            <svg viewBox="0 0 24 24" fill="none" width="15" height="15">
                <path d="M20 6L9 17l-5-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            Test Cookies
        `;
    }
}

// Add event listener (place this in your DOMContentLoaded or at the bottom)
document.addEventListener('DOMContentLoaded', function() {
    // ... existing code ...
    
    const btnTestCookies = document.getElementById('btn-test-cookies');
    if (btnTestCookies) {
        btnTestCookies.addEventListener('click', testCookies);
    }
});

  function setQuality(value) {
    qualitySelect.value = value;
    if (qualityPills) {
      qualityPills.querySelectorAll('.quality-pill').forEach((btn) => {
        btn.classList.toggle('is-active', btn.dataset.value === value);
      });
    }
    // If info for a single video is already showing, refresh its size
    // estimate for the newly picked quality (debounced for pill taps).
    if (!infoCard.hidden && urlInput.value.trim()) {
      clearTimeout(refetchTimer);
      refetchTimer = setTimeout(fetchInfo, 250);
    }
  }


  if (qualityPills) {
    qualityPills.addEventListener('click', (e) => {
      const btn = e.target.closest('.quality-pill');
      if (btn) setQuality(btn.dataset.value);
    });
  }
  qualitySelect.addEventListener('change', () => setQuality(qualitySelect.value));

  // ---------- wire up ----------

  btnFetch.addEventListener('click', fetchInfo);
  btnClear.addEventListener('click', clearUrl);
  urlInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchInfo(); });

  btnDownload.addEventListener('click', startDownload);
  btnPause.addEventListener('click', togglePause);
  btnCancel.addEventListener('click', cancelDownload);

  btnRefresh.addEventListener('click', loadDownloads);
  btnUploadCookies.addEventListener('click', uploadCookies);
  downloadsBody.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-reveal]');
    if (btn) revealFile(btn.dataset.reveal);
  });

  if (btnBrowseFolder) btnBrowseFolder.addEventListener('click', browseFolder);
  if (btnSetFolder) btnSetFolder.addEventListener('click', setFolder);

  refreshStatus();
  loadDownloads();
})();