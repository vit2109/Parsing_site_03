// Run with: node tests/leaflet_scan_check.js path/to/extracted_script.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const extract = eval(fs.readFileSync(process.argv[2], 'utf8'));
const nativeSetTimeout = global.setTimeout;
global.setTimeout = (callback, delay, ...args) =>
  nativeSetTimeout(callback, Math.min(delay, 1), ...args);

async function check(initialZoom, mapDiscoverable = true, crowded = false) {
  const points = Array.from({length: 12}, (_, i) => ({lat: 50, lng: i * 0.5, number: 101 + i}));
  const listeners = new Set();
  const map = {
    zoom: initialZoom, center: {lat: 50, lng: 0},
    getZoom() { return this.zoom; },
    getCenter() { return {...this.center}; },
    getMinZoom() { return 4; },
    getMaxZoom() { return 10; },
    on(_event, callback) { listeners.add(callback); },
    off(_event, callback) { listeners.delete(callback); },
    setView([lat, lng], zoom) {
      this.center = {lat, lng}; this.zoom = zoom;
      for (const callback of [...listeners]) callback();
    },
    latLngToContainerPoint([lat, lng]) {
      const scale = 100 * 2 ** (this.zoom - 6);
      return {x: 400 + (lng - this.center.lng) * scale, y: 300 + (this.center.lat - lat) * scale};
    },
    containerPointToLatLng({x, y}) {
      const scale = 100 * 2 ** (this.zoom - 6);
      return {lat: this.center.lat - (y - 300) / scale, lng: this.center.lng + (x - 400) / scale};
    },
  };
  const parent = {parentElement: null};
  if (mapDiscoverable) {
    parent.__vueParentComponent = {setupState: {leaflet: {value: {instance: map}}}};
  }
  const mapEl = {
    parentElement: parent,
    getBoundingClientRect: () => ({left: 0, top: 0, width: 800, height: 600}),
    querySelector: () => null,
  };
  function element(point, number) {
    const p = map.latLngToContainerPoint([point.lat, point.lng]);
    const type = 'map-incident_aircraft-drone';
    return {
      classList: number ? ['leaflet-marker-icon', 'map-incident-operator-number']
        : ['leaflet-marker-icon', 'map-incident', type],
      dataset: {incidentId: String(point.number)},
      id: '', textContent: number ? String(point.number) : '',
      getBoundingClientRect: () => ({left: p.x - 10, top: p.y - 10, width: 20, height: 20}),
      getAttribute: (name) => name === 'style'
        ? `transform: translate3d(${p.x}px, ${p.y}px, 0px)` : '',
      querySelector: () => null,
    };
  }
  global.window = {innerWidth: 800, innerHeight: 600};
  global.document = {
    querySelector: () => mapEl,
    querySelectorAll(selector) {
      const visible = points.filter((point) => {
        const p = map.latLngToContainerPoint([point.lat, point.lng]);
        return p.x >= 0 && p.x <= 800 && p.y >= 0 && p.y <= 600;
      });
      if (selector === '.leaflet-marker-icon.map-incident') return visible.map((p) => element(p, false));
      if (selector === '.leaflet-marker-icon.map-incident-operator-number')
        return map.zoom >= 8
          ? visible.filter((p) => !crowded || map.zoom >= 10 || p.number % 2 === 0)
              .map((p) => element(p, true)) : [];
      return [];
    },
  };
  global.getComputedStyle = () => ({backgroundColor: 'red'});
  const result = await extract({
    mode: 'latlng', mapContainer: '.leaflet-container',
    dynamic: '.leaflet-marker-icon.map-incident', static: '.static',
    staticObjects: [], scanNumbers: true,
  });
  if (!mapDiscoverable) {
    assert.equal(result.scan.status, 'no_leaflet_instance');
    assert.equal(result.unnumbered_dynamics,
      document.querySelectorAll('.leaflet-marker-icon.map-incident').length);
    return;
  }
  assert.deepEqual(result.dynamics.map((item) => item.index).sort((a, b) => a - b),
    points.map((point) => point.number));
  assert.equal(result.unnumbered_dynamics, 0);
  assert.equal(result.scan.status, 'complete');
  assert.deepEqual(map.center, {lat: 50, lng: 0});
  assert.equal(map.zoom, initialZoom);
}

(async () => {
  await check(6); // все маркеры видны, номера скрыты
  await check(8); // номера видны, часть маркеров за пределами окна
  await check(6, true, true); // скопление: часть подписей появится лишь на следующем масштабе
  await check(6, false); // диагностика, когда карта не найдена
  console.log('Leaflet scan: both zoom cases passed');
})().catch((error) => {console.error(error); process.exitCode = 1;});
