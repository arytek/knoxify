// Knoxify — frontend.
//
// - Leaflet map with leaflet-draw for selecting the bbox.
// - Area stats update live as the rectangle is drawn/edited.
// - POSTs to /api/generate and renders results.

const MAX_AREA_KM2 = 20.0;

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

map.on(L.Draw.Event.CREATED, (e) => {
  drawnItems.clearLayers();
  currentRect = e.layer;
  drawnItems.addLayer(currentRect);
  updateBboxFields();
});
map.on(L.Draw.Event.EDITED, () => updateBboxFields());
map.on(L.Draw.Event.DELETED, () => {
  currentRect = null;
  clearBboxFields();
});

function updateBboxFields() {
  if (!currentRect) return clearBboxFields();
  const b = currentRect.getBounds();
  const s = b.getSouth(), w = b.getWest(), n = b.getNorth(), e = b.getEast();
  document.getElementById('south').value = s.toFixed(6);
  document.getElementById('west').value  = w.toFixed(6);
  document.getElementById('north').value = n.toFixed(6);
  document.getElementById('east').value  = e.toFixed(6);

  const area = bboxAreaKm2(s, w, n, e);
  const mpt = parseFloat(document.getElementById('metersPerTile').value);
  const widthM  = haversineKm(s, w, s, e) * 1000;
  const heightM = haversineKm(s, w, n, w) * 1000;
  const tilesX = Math.ceil(widthM / mpt / 300) * 300;
  const tilesY = Math.ceil(heightM / mpt / 300) * 300;
  const cellsX = tilesX / 300;
  const cellsY = tilesY / 300;

  const stats = document.getElementById('area-stats');
  stats.innerHTML = `
    <div><strong>${area.toFixed(2)} km²</strong> selected</div>
    <div>~${Math.round(widthM)} × ${Math.round(heightM)} m</div>
    <div>Output: <strong>${tilesX} × ${tilesY}</strong> tiles
      (${cellsX} × ${cellsY} cells)</div>
  `;
  stats.className = '';
  const btn = document.getElementById('generateBtn');
  if (area > MAX_AREA_KM2) {
    stats.className = 'warn';
    stats.innerHTML += `<div>⚠ Too large (max ${MAX_AREA_KM2} km²).</div>`;
    btn.disabled = true;
  } else {
    stats.className = 'ok';
    btn.disabled = false;
  }
}

function clearBboxFields() {
  ['south', 'west', 'north', 'east'].forEach(id => {
    document.getElementById(id).value = '';
  });
  document.getElementById('area-stats').textContent = 'Draw a rectangle to see stats.';
  document.getElementById('area-stats').className = '';
  document.getElementById('generateBtn').disabled = true;
}

document.getElementById('metersPerTile').addEventListener('change', updateBboxFields);

function bboxAreaKm2(s, w, n, e) {
  const hKm = (n - s) * 111.32;
  const wKm = (e - w) * 111.32 * Math.cos((s + n) / 2 * Math.PI / 180);
  return Math.abs(hKm * wKm);
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

// ---- generation ----

document.getElementById('generateBtn').addEventListener('click', async () => {
  if (!currentRect) return;
  const b = currentRect.getBounds();
  const body = {
    south: b.getSouth(),
    west: b.getWest(),
    north: b.getNorth(),
    east: b.getEast(),
    metersPerTile: parseFloat(document.getElementById('metersPerTile').value),
    mapName: document.getElementById('mapName').value.trim() || null,
  };

  const btn = document.getElementById('generateBtn');
  const status = document.getElementById('status');
  btn.disabled = true;
  status.className = '';
  status.textContent = 'Querying OpenStreetMap and rendering bitmaps… this can take 10–60 s.';

  try {
    const res = await fetch('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);

    status.className = 'success';
    status.textContent = `Done in ${data.osmSeconds}s (OSM query). ${data.featureCount} features rendered.`;
    renderResults(data);
  } catch (err) {
    status.className = 'error';
    status.textContent = `Error: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
});

function renderResults(data) {
  const section = document.getElementById('results');
  section.hidden = false;
  document.getElementById('previewImg').src = data.files.preview + '?t=' + Date.now();
  document.getElementById('previewLink').href = data.files.preview;

  document.getElementById('results-info').innerHTML = `
    <div><strong>Name:</strong> <code>${data.mapName}</code></div>
    <div><strong>Size:</strong> ${data.width} × ${data.height} tiles</div>
    <div><strong>Cells:</strong> ${data.cellsX} × ${data.cellsY}</div>
    <div><strong>OSM features:</strong> ${data.featureCount}</div>
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
  section.scrollIntoView({ behavior: 'smooth' });
}
