// Knoxify — frontend.
//
// - Leaflet map with leaflet-draw for selecting the bbox.
// - Area stats update live as the rectangle is drawn/edited.
// - Starts background exports and polls their actual progress.

const CELL_SIZE = 300;
const OVERPASS_CHUNK_AREA_KM2 = 12.0;
const LARGE_AREA_WARNING_KM2 = 100.0;
const LARGE_SIDE_WARNING_TILES = 6000;

const map = L.map('map', { zoomControl: true }).setView([38.0406, -84.5037], 14);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '© OpenStreetMap contributors',
}).addTo(map);

const drawnItems = new L.FeatureGroup().addTo(map);
const drawControl = new L.Control.Draw({
  draw: {
    polygon: false, polyline: false, circle: false, marker: false,
    circlemarker: false,
    rectangle: { shapeOptions: { color: '#a5e266', weight: 2 } },
  },
  edit: { featureGroup: drawnItems, remove: true },
});
map.addControl(drawControl);

let currentRect = null;
let generationBusy = false;
let activeJobId = null;
let elapsedTimer = null;
let elapsedOrigin = 0;
let packBusy = false;
let activePackJobId = null;
let recommendedPack = null;
let offlinePackForSelection = null;
let packCheckTimer = null;
let packCheckSequence = 0;

function setCurrentRect(bounds, fit = true) {
  drawnItems.clearLayers();
  currentRect = L.rectangle(bounds, {
    color: '#a5e266',
    weight: 2,
    fillOpacity: 0.08,
  });
  drawnItems.addLayer(currentRect);
  updateBboxFields();
  if (fit) {
    map.fitBounds(bounds, { padding: [24, 24] });
  }
}

map.on(L.Draw.Event.CREATED, (e) => {
  setCurrentRect(e.layer.getBounds(), false);
});
map.on(L.Draw.Event.EDITED, () => updateBboxFields());
map.on(L.Draw.Event.DELETED, () => {
  currentRect = null;
  clearBboxFields();
});

function updateBboxFields(checkPacks = true) {
  if (!currentRect) return clearBboxFields();
  const b = currentRect.getBounds();
  const s = b.getSouth(), w = b.getWest(), n = b.getNorth(), e = b.getEast();
  document.getElementById('south').value = s.toFixed(6);
  document.getElementById('west').value  = w.toFixed(6);
  document.getElementById('north').value = n.toFixed(6);
  document.getElementById('east').value  = e.toFixed(6);

  const mpt = parseFloat(document.getElementById('metersPerTile').value);
  const selection = selectionStats(s, w, n, e, mpt);
  const chunkCount = Math.max(1, Math.ceil(selection.area / OVERPASS_CHUNK_AREA_KM2));

  const stats = document.getElementById('area-stats');
  stats.innerHTML = `
    <div><strong>${selection.area.toFixed(2)} km²</strong> selected</div>
    <div>~${Math.round(selection.widthM)} × ${Math.round(selection.heightM)} m</div>
    <div>Output: <strong>${selection.tilesX} × ${selection.tilesY}</strong> tiles
      (${selection.cellsX} × ${selection.cellsY} cells)</div>
    <div>${offlinePackForSelection
      ? `Map data: <strong>offline — ${escapeHtml(offlinePackForSelection.name)}</strong>`
      : `Online areas: <strong>${chunkCount}</strong>`}</div>
  `;
  const btn = document.getElementById('generateBtn');
  btn.disabled = generationBusy || packBusy;
  const isLarge = selection.area > LARGE_AREA_WARNING_KM2 ||
    Math.max(selection.tilesX, selection.tilesY) > LARGE_SIDE_WARNING_TILES;
  if (isLarge) {
    stats.className = 'warn';
    stats.innerHTML += `<div>Large export: this may take a long time and create very large files.</div>`;
  } else {
    stats.className = 'ok';
  }
  if (checkPacks) schedulePackCheck(s, w, n, e);
}

function clearBboxFields() {
  ['south', 'west', 'north', 'east'].forEach(id => {
    document.getElementById(id).value = '';
  });
  document.getElementById('area-stats').textContent = 'Draw a rectangle to see stats.';
  document.getElementById('area-stats').className = '';
  document.getElementById('generateBtn').disabled = true;
  offlinePackForSelection = null;
  recommendedPack = null;
  clearTimeout(packCheckTimer);
  document.getElementById('pack-status').textContent = 'Select an area to check offline coverage.';
  document.getElementById('packActionBtn').hidden = true;
}

document.getElementById('metersPerTile').addEventListener('change', updateBboxFields);

function bboxAreaKm2(s, w, n, e) {
  const hKm = (n - s) * 111.32;
  const wKm = (e - w) * 111.32 * Math.cos((s + n) / 2 * Math.PI / 180);
  return Math.abs(hKm * wKm);
}

function selectionStats(s, w, n, e, metersPerTile) {
  const area = bboxAreaKm2(s, w, n, e);
  const widthM = haversineKm(s, w, s, e) * 1000;
  const heightM = haversineKm(s, w, n, w) * 1000;
  const tilesX = Math.ceil(widthM / metersPerTile / CELL_SIZE) * CELL_SIZE;
  const tilesY = Math.ceil(heightM / metersPerTile / CELL_SIZE) * CELL_SIZE;
  return {
    area,
    widthM,
    heightM,
    tilesX,
    tilesY,
    cellsX: tilesX / CELL_SIZE,
    cellsY: tilesY / CELL_SIZE,
  };
}

function isSelectionAllowed(selection) {
  return Number.isFinite(selection.area) &&
    Number.isFinite(selection.tilesX) &&
    Number.isFinite(selection.tilesY);
}

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371;
  const toRad = d => d * Math.PI / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat/2)**2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon/2)**2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

function clampCellInput(id) {
  const input = document.getElementById(id);
  const value = Math.max(1, parseInt(input.value, 10) || 1);
  input.value = value;
  return value;
}

function setSelectionAround(lat, lon, fit = true) {
  const cellsWide = clampCellInput('cellsWide');
  const cellsTall = clampCellInput('cellsTall');
  const metersPerTile = parseFloat(document.getElementById('metersPerTile').value);
  const halfWidthM = cellsWide * CELL_SIZE * metersPerTile / 2;
  const halfHeightM = cellsTall * CELL_SIZE * metersPerTile / 2;
  const latDelta = halfHeightM / 111320;
  const lonScale = 111320 * Math.max(0.1, Math.cos(lat * Math.PI / 180));
  const lonDelta = halfWidthM / lonScale;
  const bounds = L.latLngBounds(
    [lat - latDelta, lon - lonDelta],
    [lat + latDelta, lon + lonDelta],
  );
  setCurrentRect(bounds, fit);
}

document.getElementById('presetBtn').addEventListener('click', () => {
  const center = map.getCenter();
  setSelectionAround(center.lat, center.lng);
});

document.getElementById('placeSearch').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    searchPlaces();
  }
});

document.getElementById('searchBtn').addEventListener('click', searchPlaces);

async function searchPlaces() {
  const input = document.getElementById('placeSearch');
  const query = input.value.trim();
  const status = document.getElementById('search-status');
  const list = document.getElementById('search-results');
  if (!query) return;

  status.className = '';
  status.textContent = 'Searching...';
  list.innerHTML = '';

  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(query)}&limit=5`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    renderPlaceResults(data.results || []);
    status.textContent = data.results?.length ? '' : 'No places found.';
  } catch (err) {
    status.className = 'error';
    status.textContent = `Error: ${err.message}`;
  }
}

function renderPlaceResults(results) {
  const list = document.getElementById('search-results');
  list.innerHTML = '';
  results.forEach(place => {
    const li = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';

    const title = document.createElement('span');
    title.textContent = place.displayName;
    button.appendChild(title);

    const detail = document.createElement('small');
    detail.textContent = [place.category, place.type].filter(Boolean).join(' / ');
    button.appendChild(detail);

    button.addEventListener('click', () => usePlace(place));
    li.appendChild(button);
    list.appendChild(li);
  });
}

function usePlace(place) {
  const placeBounds = L.latLngBounds(
    [place.south, place.west],
    [place.north, place.east],
  );
  const mpt = parseFloat(document.getElementById('metersPerTile').value);
  const stats = selectionStats(place.south, place.west, place.north, place.east, mpt);
  if (isSelectionAllowed(stats)) {
    setCurrentRect(placeBounds);
  } else {
    setSelectionAround(place.lat, place.lon);
  }
}

// ---- generation ----

document.getElementById('generateBtn').addEventListener('click', async () => {
  if (!currentRect || generationBusy) return;
  const b = currentRect.getBounds();
  const body = {
    south: b.getSouth(),
    west: b.getWest(),
    north: b.getNorth(),
    east: b.getEast(),
    metersPerTile: parseFloat(document.getElementById('metersPerTile').value),
    mapName: document.getElementById('mapName').value.trim() || null,
  };

  const status = document.getElementById('status');
  setGenerating(true);
  document.getElementById('results').hidden = true;
  showProgress({ progress: 0, message: 'Preparing export', elapsedSeconds: 0, state: 'running' });
  status.className = '';
  status.textContent = '';

  try {
    const data = await requestJson('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    await followJob(data.jobId || data.id);
  } catch (err) {
    // A response can be lost after the server accepted the export.
    try {
      const current = await requestJson('/api/jobs/active');
      if (current.id) {
        await followJob(current.id);
        return;
      }
    } catch (_) { /* The server is unavailable; keep the original error. */ }
    status.className = 'error';
    status.textContent = `Error: ${err.message}`;
    document.getElementById('progress-detail').textContent = 'Could not start export';
  } finally {
    setGenerating(false);
  }
});

async function requestJson(url, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal, cache: 'no-store' });
    const data = await response.json();
    if (!response.ok && !(response.status === 409 && data.jobId)) {
      const error = new Error(data.error || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return data;
  } finally {
    clearTimeout(timeout);
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[character]);
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function schedulePackCheck(south, west, north, east) {
  clearTimeout(packCheckTimer);
  const sequence = ++packCheckSequence;
  packCheckTimer = setTimeout(() => checkPackCoverage({ south, west, north, east }, sequence), 500);
}

async function checkPackCoverage(bounds, sequence = ++packCheckSequence) {
  const status = document.getElementById('pack-status');
  status.className = '';
  status.textContent = 'Checking regional data...';
  try {
    const query = new URLSearchParams(bounds).toString();
    const data = await requestJson(`/api/packs/status?${query}`);
    if (sequence !== packCheckSequence) return;
    recommendedPack = data.recommended;
    offlinePackForSelection = data.installedCovering;
    renderPackStatus(data);
    if (currentRect) updateBboxFields(false);
  } catch (err) {
    if (sequence !== packCheckSequence) return;
    recommendedPack = null;
    offlinePackForSelection = null;
    status.className = 'error';
    status.textContent = `Regional data check unavailable. Online generation still works.`;
    document.getElementById('packActionBtn').hidden = true;
  }
}

function renderPackStatus(data) {
  const status = document.getElementById('pack-status');
  const action = document.getElementById('packActionBtn');
  status.className = '';
  action.hidden = true;
  action.disabled = packBusy || generationBusy;
  if (data.installedCovering) {
    status.className = 'ready';
    status.textContent = `${data.installedCovering.name} is ready. This selection will use local map data.`;
    if (data.recommended?.id === data.installedCovering.id) {
      action.textContent = 'Update regional data';
      action.hidden = false;
    }
  } else if (data.recommended) {
    status.textContent = `${data.recommended.name} is available as a one-time regional download.`;
    action.textContent = data.recommended.installed ? 'Update regional data' : 'Download regional data';
    action.hidden = false;
  } else if (data.catalogError) {
    status.className = 'error';
    status.textContent = 'Could not check regional downloads. Online generation remains available.';
  } else {
    status.textContent = 'No single regional download covers this selection. Online map data will be used.';
  }
  renderInstalledPacks(data.installed || []);
}

function renderInstalledPacks(packs) {
  const list = document.getElementById('installed-packs');
  list.innerHTML = '';
  packs.forEach(pack => {
    const item = document.createElement('li');
    const label = document.createElement('span');
    const size = formatBytes(pack.stored_bytes);
    label.textContent = `${pack.name}${size ? ` · ${size}` : ''}`;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = 'Remove';
    remove.disabled = packBusy || generationBusy;
    remove.addEventListener('click', () => removePack(pack));
    item.append(label, remove);
    list.appendChild(item);
  });
}

document.getElementById('packActionBtn').addEventListener('click', async () => {
  if (!recommendedPack || packBusy || generationBusy) return;
  setPackBusy(true);
  try {
    const data = await requestJson('/api/packs/install', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ regionId: recommendedPack.id, refresh: recommendedPack.installed }),
    });
    await followPackJob(data.jobId || data.id);
  } catch (err) {
    document.getElementById('pack-status').className = 'error';
    document.getElementById('pack-status').textContent = `Regional download failed: ${err.message}`;
  } finally {
    setPackBusy(false);
  }
});

function setPackBusy(busy) {
  packBusy = busy;
  document.getElementById('packActionBtn').disabled = busy || generationBusy;
  document.getElementById('packCancelBtn').hidden = !busy;
  document.getElementById('generateBtn').disabled = busy || generationBusy || !currentRect;
}

function showPackProgress(job) {
  document.getElementById('pack-progress').hidden = false;
  document.getElementById('pack-progress-bar').value = job.progress;
  document.getElementById('pack-progress-percent').textContent = `${Math.floor(job.progress)}%`;
  document.getElementById('pack-progress-detail').textContent = job.message;
  document.getElementById('packCancelBtn').disabled = job.state === 'cancelling';
}

async function followPackJob(id) {
  activePackJobId = id;
  try { localStorage.setItem('knoxify.packJob', id); } catch (_) { /* Storage disabled. */ }
  setPackBusy(true);
  while (activePackJobId === id) {
    let job;
    try {
      job = await requestJson(`/api/packs/jobs/${id}`);
    } catch (err) {
      document.getElementById('pack-progress-detail').textContent = 'Connection interrupted. Reconnecting...';
      await new Promise(resolve => setTimeout(resolve, 3000));
      continue;
    }
    showPackProgress(job);
    if (['complete', 'failed', 'cancelled'].includes(job.state)) {
      try { localStorage.removeItem('knoxify.packJob'); } catch (_) { /* Storage disabled. */ }
      activePackJobId = null;
      setPackBusy(false);
      const status = document.getElementById('pack-status');
      if (job.state === 'complete') {
        status.className = 'ready';
        status.textContent = `${job.result.name} is ready for offline generation.`;
        document.getElementById('pack-progress').hidden = true;
      } else {
        status.className = job.state === 'failed' ? 'error' : '';
        status.textContent = job.error || 'Regional download cancelled. You can resume it later.';
      }
      if (currentRect) updateBboxFields();
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
}

document.getElementById('packCancelBtn').addEventListener('click', async () => {
  if (!activePackJobId) return;
  document.getElementById('packCancelBtn').disabled = true;
  try {
    showPackProgress(await requestJson(`/api/packs/jobs/${activePackJobId}/cancel`, { method: 'POST' }));
  } catch (err) {
    document.getElementById('pack-status').textContent = `Could not cancel: ${err.message}`;
  }
});

async function removePack(pack) {
  if (!confirm(`Remove the downloaded ${pack.name} regional data?`)) return;
  try {
    await requestJson('/api/packs/remove', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ regionId: pack.id }),
    });
    if (currentRect) updateBboxFields();
  } catch (err) {
    document.getElementById('pack-status').className = 'error';
    document.getElementById('pack-status').textContent = `Could not remove regional data: ${err.message}`;
  }
}

async function restorePackJob() {
  let saved;
  try { saved = localStorage.getItem('knoxify.packJob'); } catch (_) { /* Storage disabled. */ }
  try {
    if (!saved) saved = (await requestJson('/api/packs/jobs/active')).id;
    if (saved) await followPackJob(saved);
  } catch (_) {
    try { localStorage.removeItem('knoxify.packJob'); } catch (_) { /* Storage disabled. */ }
    setPackBusy(false);
  }
}

async function loadInstalledPacks() {
  try {
    const data = await requestJson('/api/packs');
    renderInstalledPacks(data.installed || []);
  } catch (_) { /* Online generation does not depend on the pack list. */ }
}

function rememberJob(id) {
  try {
    if (id) localStorage.setItem('knoxify.activeJob', id);
    else localStorage.removeItem('knoxify.activeJob');
  } catch (_) { /* Generation still works when browser storage is unavailable. */ }
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

function setGenerating(busy) {
  generationBusy = busy;
  document.getElementById('generateBtn').disabled = busy || packBusy || !currentRect;
  document.getElementById('generateBtn').textContent = busy ? 'Generating...' : 'Generate map';
  document.getElementById('cancelBtn').hidden = !busy;
  if (!busy) {
    clearInterval(elapsedTimer);
    activeJobId = null;
  }
}

function showProgress(job) {
  document.getElementById('generation-progress').hidden = false;
  document.getElementById('generation-bar').value = job.progress;
  document.getElementById('progress-percent').textContent = `${Math.floor(job.progress)}%`;
  const detail = document.getElementById('progress-detail');
  if (detail.textContent !== job.message) detail.textContent = job.message;
  elapsedOrigin = Date.now() - job.elapsedSeconds * 1000;
  document.getElementById('progress-elapsed').textContent = `${formatDuration(job.elapsedSeconds)} elapsed`;
  document.getElementById('cancelBtn').disabled = !activeJobId || job.state === 'cancelling';
}

async function followJob(id) {
  activeJobId = id;
  rememberJob(id);
  setGenerating(true);
  elapsedOrigin = Date.now();
  clearInterval(elapsedTimer);
  elapsedTimer = setInterval(() => {
    document.getElementById('progress-elapsed').textContent =
      `${formatDuration((Date.now() - elapsedOrigin) / 1000)} elapsed`;
  }, 1000);
  let failures = 0;
  try {
    while (activeJobId === id) {
      let job;
      try {
        job = await requestJson(`/api/jobs/${id}`);
        failures = 0;
      } catch (err) {
        if (err.status === 404) {
          rememberJob(null);
          throw err;
        }
        failures += 1;
        document.getElementById('progress-detail').textContent = 'Connection interrupted. Reconnecting...';
        await new Promise(resolve => setTimeout(resolve, Math.min(10000, failures * 2000)));
        continue;
      }
      showProgress(job);
      if (['complete', 'failed', 'cancelled'].includes(job.state)) {
        rememberJob(null);
        const status = document.getElementById('status');
        if (job.state === 'complete') {
          status.className = 'success';
          status.textContent = `Done in ${formatDuration(job.result.totalSeconds)}. ${job.result.featureCount} features rendered.`;
          renderResults(job.result);
        } else {
          status.className = job.state === 'failed' ? 'error' : '';
          status.textContent = job.error || 'Cancelled. Completed map-data areas are saved for your next attempt.';
        }
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
  } finally {
    setGenerating(false);
  }
}

document.getElementById('cancelBtn').addEventListener('click', async () => {
  if (!activeJobId) return;
  const button = document.getElementById('cancelBtn');
  button.disabled = true;
  try {
    showProgress(await requestJson(`/api/jobs/${activeJobId}/cancel`, { method: 'POST' }));
  } catch (err) {
    button.disabled = false;
    document.getElementById('status').textContent = `Could not cancel: ${err.message}`;
  }
});

async function restoreJob() {
  setGenerating(true);
  let saved;
  try { saved = localStorage.getItem('knoxify.activeJob'); } catch (_) { /* Storage disabled. */ }
  try {
    if (!saved) saved = (await requestJson('/api/jobs/active')).id;
    if (saved) await followJob(saved);
  } catch (err) {
    document.getElementById('status').textContent = err.message;
  } finally {
    setGenerating(false);
  }
}

function renderResults(data) {
  const section = document.getElementById('results');
  section.hidden = false;
  document.getElementById('previewImg').src = data.files.preview + '?t=' + Date.now();
  document.getElementById('previewLink').href = data.files.preview;

  const fetchStats = data.fetch || {};
  const fetchInfo = fetchStats.source === 'offline' ? `
    <div><strong>Source:</strong> Offline — ${escapeHtml(fetchStats.pack_name)}</div>
  ` : fetchStats.chunks_total ? `
    <div><strong>OSM chunks:</strong> ${fetchStats.chunks_total}
      (${fetchStats.chunks_from_cache} from cache)</div>
    <div><strong>HTTP requests:</strong> ${fetchStats.http_requests}
      (${fetchStats.retries} retries)</div>
  ` : '';
  document.getElementById('results-info').innerHTML = `
    <div><strong>Name:</strong> <code>${escapeHtml(data.mapName)}</code></div>
    <div><strong>Size:</strong> ${data.width} × ${data.height} tiles</div>
    <div><strong>Cells:</strong> ${data.cellsX} × ${data.cellsY}</div>
    <div><strong>OSM features:</strong> ${data.featureCount}</div>
    ${fetchInfo}
    <div><strong>Map data:</strong> ${formatDuration(data.timings.fetch)}</div>
    <div><strong>Rendering:</strong> ${formatDuration(data.timings.render)}</div>
    <div><strong>Packaging:</strong> ${formatDuration(data.timings.package)}</div>
    <div><strong>Total:</strong> ${formatDuration(data.totalSeconds)}</div>
  `;

  const entries = [
    ['ZIP (all BMPs + README)', data.files.zip],
    ['Landscape BMP', data.files.landscape],
    ['Vegetation BMP', data.files.vegetation],
    ['Zombie spawn BMP', data.files.spawn],
    ['Preview PNG', data.files.preview],
    ['Building footprints (GeoJSON)', data.files.buildings],
    ['Meta (JSON)', data.files.meta],
    ['README', data.files.readme],
  ];
  const ul = document.getElementById('downloads');
  ul.innerHTML = entries.map(([label, href]) =>
    `<li>→ <a href="${href}" target="_blank" download>${label}</a></li>`
  ).join('');
  section.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
}

restoreJob();
restorePackJob();
loadInstalledPacks();
